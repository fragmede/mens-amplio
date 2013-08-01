#!/usr/bin/env python

import math
import random
import noise
import numpy
import colorsys
import time

class EffectParameters(object):
    """Inputs to the individual effect layers. Includes basics like the timestamp of the frame we're
       generating, as well as parameters that may be used to control individual layers in real-time.
       """

    time = 0
    targetFrameRate = 45.0     # XXX: Want to go higher, but gl_server can't keep up!
    eeg = None


class EffectLayer(object):
    """Abstract base class for one layer of an LED light effect. Layers operate on a shared framebuffer,
       adding their own contribution to the buffer and possibly blending or overlaying with data from
       prior layers.

       The 'frame' passed to each render() function is an array of LEDs in the same order as the
       IDs recognized by the 'model' object. Each LED is a 3-element list with the red, green, and
       blue components each as floating point values with a normalized brightness range of [0, 1].
       If a component is beyond this range, it will be clamped during conversion to the hardware
       color format.
       """

    def render(self, model, params, frame):
        raise NotImplementedError("Implement render() in your EffectLayer subclass")


class HeadsetResponsiveEffectLayer(EffectLayer):
    """A layer effect that responds to the MindWave headset in some way.

    Two major differences from EffectLayer:
    1) Constructor expects two paramters:
       -- respond_to: the name of a field in EEGInfo (threads.HeadsetThread.EEGInfo).
          Currently this means either 'attention' or 'meditation'
       -- smooth_response_over_n_secs: to avoid rapid fluctuations from headset
          noise, averages the response metric over this many seconds
    2) Subclasses now only implement the render_responsive() function, which
       is the same as EffectLayer's render() function but has one extra
       parameter, response_level, which is the current EEG value of the indicated
       field (assumed to be on a 0-1 scale, or None if no value has been read yet).
    """
    def __init__(self, respond_to, smooth_response_over_n_secs=5):
        # Name of the eeg field to influence this effect
        self.respond_to = respond_to
        self.smooth_response_over_n_secs = smooth_response_over_n_secs
        self.measurements = []
        self.timestamps = []
        self.last_eeg = None
        self.last_response_level = None
        # We want to smoothly transition between values instead of jumping
        # (as the headset typically gives one reading per second)
        self.fading_to = None

    def start_fade(self, new_level):
        if not self.last_response_level:
            self.last_response_level = new_level
        else:
            self.fading_to = new_level

    def end_fade(self):
        self.last_response_level = self.fading_to
        self.fading_to = None

    def render(self, model, params, frame):
        now = time.time()
        response_level = None
        # Update our measurements, if we have a new one
        if params.eeg and params.eeg != self.last_eeg and params.eeg.on:
            if self.fading_to:
                self.end_fade()
            # Prepend newest measurement and timestamp
            self.measurements[:0] = [getattr(params.eeg, self.respond_to)]
            self.timestamps[:0] = [now]
            self.last_eeg = params.eeg
            # Compute the parameter to send to our rendering function
            N = len(self.measurements)
            idx = 0
            while idx < N:
                dt = self.timestamps[0] - self.timestamps[idx]
                if dt > self.smooth_response_over_n_secs:
                    self.measurements = self.measurements[:(idx + 1)]
                    self.timestamps = self.timestamps[:(idx + 1)]
                    break
                idx += 1
            if len(self.measurements) > 1:
                self.start_fade(sum(self.measurements) * 1.0 / len(self.measurements))
            response_level = self.last_response_level
        elif self.fading_to:
            # We assume one reading per second, so a one-second fade
            fade_progress = now - self.timestamps[0]
            if fade_progress >= 1:
                self.end_fade()
                response_level = self.last_response_level
            else:
                response_level = (
                    fade_progress * self.fading_to +
                    (1 - fade_progress) * self.last_response_level)

        self.render_responsive(model, params, frame, response_level)

    def render_responsive(self, model, params, frame, response_level):
        raise NotImplementedError(
            "Implement render_responsive() in your HeadsetResponsiveEffectLayer subclass")


class RGBLayer(EffectLayer):
    """Simplest layer, draws a static RGB color cube."""

    def render(self, model, params, frame):
        for i, rgb in enumerate(frame):
            # Normalized XYZ in the range [0,1]
            x, y, z = model.edgeCenters[i]
            rgb[0] = x
            rgb[1] = y
            rgb[2] = z


class ResponsiveGreenHighRedLow(HeadsetResponsiveEffectLayer):
    """Colors everything green if the response metric is high, red if low.

    Interpolates in between.
    """

    def render_responsive(self, model, params, frame, response_level):
        for i, rgb in enumerate(frame):
            if response_level is None:
                mixAdd(rgb, 0, 0, 1)
            else:
                mixAdd(rgb, 1 - response_level, response_level, 0)


def mixAdd(rgb, r, g, b):
    """Mix a new color with the existing RGB list by adding each component."""
    rgb[0] += r
    rgb[1] += g
    rgb[2] += b


def mixMultiply(rgb, r, g, b):    
    """Mix a new color with the existing RGB list by multiplying each component."""
    rgb[0] *= r
    rgb[1] *= g
    rgb[2] *= b


class BlinkyLayer(EffectLayer):
    """Test our timing accuracy: Just blink everything on and off every other frame."""

    on = False

    def render(self, model, params, frame):
        self.on = not self.on
        if self.on:
            for i, rgb in enumerate(frame):
                mixAdd(rgb, 1, 1, 1)


class PlasmaLayer(EffectLayer):
    """A plasma cloud layer, implemented with smoothed noise."""

    def __init__(self, zoom = 0.6, color=(1,0,0)):
        self.zoom = zoom
        self.octaves = 3
        self.color = numpy.array(color)
        self.time_const = -1.5
        self.modelCache = None
        self.ufunc = numpy.frompyfunc(noise.pnoise3, 4, 1)

    def render(self, model, params, frame):
        # Noise spatial scale, in number of noise datapoints at the fundamental frequency
        # visible along the length of the sculpture. Larger numbers "zoom out".
        # For perlin noise, we have multiple octaves of detail, so staying zoomed in lets
        # us have a lot of detail from the higher octaves while still having gradual overall
        # changes from the lower-frequency noise.

        s = self.zoom # defaults to 0.6

        # Time-varying vertical offset. "Flow" upwards, slowly. To keep the parameters to
        # pnoise3() in a reasonable range where conversion to single-precision float within
        # the module won't be a problem, we need to wrap the coordinates at the point where
        # the noise function seamlessly tiles. By default, this is at 1024 units in the
        # coordinate space used by pnoise3().

        z0 = math.fmod(params.time * self.time_const, 1024.0)

        # Cached values based on the current model
        if model is not self.modelCache:
            self.modelCache = model
            self.scaledX = s * model.edgeCenters[:,0]
            self.scaledY = s * model.edgeCenters[:,1]
            self.scaledZ = s * model.edgeCenters[:,2]

        # Compute noise values at the center of each edge
        noise = self.ufunc(self.scaledX, self.scaledY, self.scaledZ + z0, self.octaves)

        # Brightness scaling
        numpy.add(noise, 0.35, noise)
        numpy.multiply(noise, 1.2, noise)

        # Multiply by color, accumulate into current frame
        numpy.add(frame, self.color * noise.reshape(-1, 1), frame)


class WavesLayer(EffectLayer):
    """Occasional wavefronts of light which propagate outward from the base of the tree"""

    color = numpy.array((0.5, 0.5, 1.0))
    width = 0.4
    speed = 1.5
    period = 15.0

    def render(self, model, params, frame):

        # Center of the expanding wavefront
        center = math.fmod(params.time * self.speed, self.period)

        # Only do the rest of the calculation if the wavefront is at all visible.
        if center < 2.0:

            # Calculate each pixel's position within the pulse, in radians
            a = model.edgeDistances - center
            numpy.abs(a, a)
            numpy.multiply(a, math.pi/2 / self.width, a)

            # Clamp against the edge of the pulse
            numpy.minimum(a, math.pi/2, a)

            # Pulse shape
            numpy.cos(a, a)

            # Colorize
            numpy.add(frame, a.reshape(-1,1) * self.color, frame)


class ImpulsesLayer(EffectLayer):
    """Oscillating neural impulses which travel outward along the tree"""

    def __init__(self, count=10):
        self.positions = [None] * count
        self.phases = [0] * count
        self.frequencies = [0] * count

    def render(self, model, params, frame):
        for i in range(len(self.positions)):

            if self.positions[i] is None:
                # Impulse is dead. Random chance of reviving it.
                if random.random() < 0.05:

                    # Initialize a new impulse with some random parameters
                    self.positions[i] = random.choice(model.roots)
                    self.phases[i] = random.uniform(0, math.pi * 2)
                    self.frequencies[i] = random.uniform(2.0, 10.0)

            else:
                # Draw the impulse
                br = max(0, math.sin(self.phases[i] + self.frequencies[i] * params.time))
                mixAdd(frame[self.positions[i]], br, br, br)

                # Chance of moving this impulse outward
                if random.random() < 0.2:

                    choices = model.outwardAdjacency[i]
                    if choices:
                        self.positions[i] = random.choice(choices)
                    else:
                        # End of the line
                        self.positions[i] = None


class DigitalRainLayer(EffectLayer):
    """Sort of look like The Matrix"""
    def __init__(self):
        self.tree_count = 6
        self.period = math.pi * 2
        self.maxoffset = self.period
        self.offsets = [ self.maxoffset * n / self.tree_count for n in range(self.tree_count) ]
        self.speed = 2
        self.height = 1/3.0

        random.shuffle(self.offsets)
        self.offsets = numpy.array(self.offsets)

        self.color = numpy.array([v/255.0 for v in [90, 210, 90]])
        self.bright = numpy.array([v/255.0 for v in [140, 234, 191]])

        # Build a color table across one period
        self.colorX = numpy.arange(0, self.period, self.period / 100)
        self.colorY = numpy.array([self.calculateColor(x) for x in self.colorX])

    def calculateColor(self, v):
        # Bright part
        if v < math.pi / 4:
            return self.bright

        # Nonlinear fall-off
        if v < math.pi:
            return self.color * (math.sin(v) ** 2)

        # Empty
        return [0,0,0]

    def render(self, model, params, frame):

        # Scalar animation parameter, based on height and distance
        d = model.edgeCenters[:,2] + 0.5 * model.edgeDistances
        numpy.multiply(d, 1/self.height, d)

        # Add global offset for Z scrolling over time
        numpy.add(d, params.time * self.speed, d)

        # Add an offset that depends on which tree we're in
        numpy.add(d, numpy.choose(model.edgeTree, self.offsets), d)

        # Periodic animation, stored in our color table. Linearly interpolate.
        numpy.fmod(d, self.period, d)
        color = numpy.empty((model.numLEDs, 3))
        color[:,0] = numpy.interp(d, self.colorX, self.colorY[:,0])
        color[:,1] = numpy.interp(d, self.colorX, self.colorY[:,1])
        color[:,2] = numpy.interp(d, self.colorX, self.colorY[:,2])

        # Random flickering noise
        noise = numpy.random.rand(model.numLEDs).reshape(-1, 1)
        numpy.multiply(noise, 0.25, noise)
        numpy.add(noise, 0.75, noise)

        numpy.multiply(color, noise, color)
        numpy.add(frame, color, frame)


class SnowstormLayer(EffectLayer):
    def render(self, model, params, frame):
        for i, rgb in enumerate(frame):
            level = random.random()
            for w, v in enumerate(rgb):
                rgb[w] += level

class TechnicolorSnowstormLayer(EffectLayer):
    def render(self, model, params, frame):
        for i, rgb in enumerate(frame):
            for w, v in enumerate(rgb):
                level = random.random()
                rgb[w] += level

class ImpulseLayer2(EffectLayer):
    class Impulse():
        def __init__(self, color, edge, motion = "Out"):
            self.color = color
            self.edge = edge
            self.previous_edge = None
            self.dead = False
            self.motion = "Out"

            self.loopChance = 0.1
            self.bounceChance = 0.2

        def _move_to_any_of(self, edges):
            self.previous_edge = self.edge
            self.edge = random.choice(edges)

        def _node_incoming_and_outgoing(self, model):
            nodes = model.edges[self.edge]
            previous_nodes = model.edges[self.previous_edge]
            from_node = [n for n in nodes if n in previous_nodes][0]
            to_node = [n for n in nodes if n != from_node][0]
            return (from_node, to_node)

        def move(self, model, params):
            height = model.edgeHeight[self.edge]
            nodes = model.edges[self.edge]
            to_edges = [e for n in nodes for e in model.edgeListForNodes[n] if e != self.edge ]

            if random.random() < self.loopChance:
                if self.motion == 'Out' and height == 4:
                    self.motion = 'Loop'
                elif self.motion == 'In' and height == 5:
                    self.motion = 'Loop'
                elif self.motion == 'Loop' and height == 5:
                    self.motion = 'Out'
                elif self.motion == 'Loop' and height == 4:
                    self.motion = 'In'

            if self.motion == 'Loop':
                in_node, out_node = self._node_incoming_and_outgoing(model)
                to_edges = [e for e in model.edgeListForNodes[out_node] if e != self.edge]
                to_edges = [e for e in to_edges if model.addressMatchesAnyP(model.addressForEdge[e], ["*.*.*.*.*", "*.*.*.*.1.2", "*.*.*.*.2.1"])]
            elif self.motion == 'Out':
                to_edges = [e for e in to_edges if model.edgeHeight[e] > height]
            elif self.motion == 'In':
                to_edges = [e for e in to_edges if model.edgeHeight[e] < height]

            if to_edges:
                self._move_to_any_of(to_edges)
            else:
                if random.random() < self.bounceChance:
                  if self.motion == 'Out':
                      self.motion = 'In'
                      self.move(model, params)
                  elif self.motion == 'In':
                      self.motion = 'Out'
                      self.move(model, params)
                  else:
                      print "Broken"
                      self.dead = True
                else:
                  self.dead = True

        def render(self, model, params, frame):
            if self.dead:
                return
            for v,c in enumerate(self.color):
                frame[self.edge][v] += c

    def __init__(self, maximum_pulse_count = 40):
        self.pulses = [None] * maximum_pulse_count
        self.last_time = None

        # these are adjustable
        self.frequency = 0.05 # seconds
        self.spawnChance = 0.25
        self.maxColorSaturation = 0.25
        self.brightness = 0.95

    def _move_pulses(self, model, params):
        if not self.last_time:
            self.last_time = params.time
            return
        if params.time < self.last_time + self.frequency:
            return
        self._reap_pulses(model, params)
        self._spawn_pulses(model, params)

        self.last_time = params.time
        for pulse in self.pulses:
            if pulse:
                pulse.move(model, params)

    def _reap_pulses(self, model, params):
        for i, p in enumerate(self.pulses):
            if p and p.dead:
                self.pulses[i] = None

    def _spawn_pulses(self, model, params):
        if random.random() < self.spawnChance:
          for i, p in enumerate(self.pulses):
              if not p:
                  if self.maxColorSaturation:
                      hue = random.random()
                      saturation = random.random() * self.maxColorSaturation
                      value = self.brightness
                      color = colorsys.hsv_to_rgb(hue, saturation, value)
                  else: # optimization for saturation 0
                      color = (self.brightness,self.brightness,self.brightness)

                  self.pulses[i] = ImpulseLayer2.Impulse(color, random.choice(model.roots))
                  return self._spawn_pulses(model, params)

    def render(self, model, params, frame):
        self._move_pulses(model, params)
        for pulse in self.pulses:
            if pulse:
                pulse.render(model, params, frame)

class Bolt(object):
    """Represents a single lightning bolt in the LightningStormLayer effect."""

    PULSE_INTENSITY = 0.08
    PULSE_FREQUENCY = 10.
    FADE_TIME = 0.25
    SECONDARY_BRANCH_INTENSITY = 0.4

    def __init__(self, model, init_time):
        self.init_time = init_time
        self.pulse_time = random.uniform(.25, .35)
        self.color = [v/255.0 for v in [230, 230, 255]]  # Violet storm
        self.life_time = self.pulse_time + Bolt.FADE_TIME
        self.edges, self.intensities = self.choose_random_path(model)

    def choose_random_path(self, model):
        leader_intensity = (1.0 - Bolt.PULSE_INTENSITY)
        branch_intensity = leader_intensity * Bolt.SECONDARY_BRANCH_INTENSITY
        root = random.choice(model.roots)
        edges = [root]
        leader = root
        intensities = [leader_intensity]
        while model.outwardAdjacency[leader]:
            next_leader = random.choice(model.outwardAdjacency[leader])
            for edge in model.outwardAdjacency[leader]:
                edges.append(edge)
                if edge == next_leader:
                    # Main bolt branch fully bright
                    intensities.append(leader_intensity)
                else:
                    # Partially light clipped branches
                    intensities.append(branch_intensity)
            leader = next_leader
        return edges, intensities

    def update_frame(self, frame, current_time):
        dt = current_time - self.init_time

        if dt < self.pulse_time:  # Bolt fully lit and pulsing
            phase = math.cos(2 * math.pi * dt * Bolt.PULSE_FREQUENCY) 
            for i, edge in enumerate(self.edges):
                mixAdd(frame[edge], *numpy.multiply(self.color,
                    self.intensities[i] + phase * Bolt.PULSE_INTENSITY))
            pass
        else:  # Bolt fades out linearly
            fade = 1 - (dt - self.pulse_time) * 1.0 / Bolt.FADE_TIME
            for i, edge in enumerate(self.edges):
                mixAdd(frame[edge], *numpy.multiply(
                    self.color, fade * self.intensities[i]))


class LightningStormLayer(EffectLayer):
    """Simulate lightning storm."""

    def __init__(self, bolt_every=.25):
        # http://www.youtube.com/watch?v=RLWIBrweSU8
        self.bolts = []
        self.bolt_every = bolt_every
        self.last_time = None

    def render(self, model, params, frame):
        if not self.last_time:
            self.last_time = params.time

        self.bolts = [bolt for bolt in self.bolts
                      if bolt.init_time + bolt.life_time > params.time]

        # Bolts will strike as a poisson arrival process. That is, randomly,
        # but on average every bolt_every seconds. The memoryless nature of it
        # will create periods of calm as well as periods of constant lightning.
        if (params.time - self.last_time) / self.bolt_every > random.random():
            # Bolts are allowed to overlap, creates some interesting effects
            self.bolts.append(Bolt(model, params.time))

        self.last_time = params.time

        for bolt in self.bolts:
            bolt.update_frame(frame, params.time)
            
            
class FireflySwarm(EffectLayer):
    """
    A group of phase-coupled fireflies. When one blinks, it pulls its neighbors closer to
    blinking themselves, which will eventually bring the whole group into sync.
    
    For a full explanation of how this works, see:
    Synchronization of Pulse-Coupled Biological Oscillators
    Renato E. Mirollo; Steven H. Strogatz
    SIAM Journal on Applied Mathematics, Vol. 50, No. 6. (Dec., 1990), pp. 1645-1662
    
    This has a subtle bug somewhere where occasionally some edges skip a blink.
    I'm putting off fixing it until we decide if we actually want to use this.
    """
    
    class Firefly:
        """
        A single firefly. Its activation level increases monotonically in range [0,1] as
        a function of time. When its activation reaches 1, it initiates a blink and drops
        back to 0.
        """
        
        CYCLE_TIME = 1.5 # seconds
        NUDGE = 0.15 # how much to nudge it toward firing after its neighbor fires
        EXP = 2.0 # exponent for phase->activation function, chosen somewhat arbitrarily
        
        def __init__(self, edge):
            self.offset = random.random() * self.CYCLE_TIME
            self.edge = edge
            self.color = (1,1,1)
            self.blinktime = 0
            
        def nudge(self, params):
            """ Bump this firefly forward in its cycle, closer to its next blink """
            p = self.phi(params)
            a = self.activation(p)
            
            # if it isn't already blinking...
            if a < 1.0:
                # new activation level, closer to (but not exceeding) blink threshold
                a2 = min(a + self.NUDGE, 1)
                # find the phase parameter corresponding to that activation level
                p2 = self.activation_to_phi(a2)
                # adjust time offset to bring us to that phase
                self.offset += max(p2 - p, 0) * self.CYCLE_TIME

                # TMI
                debug=False
                if self.edge == 66 and debug:
                    print self.offset,
                    print p,
                    print p2,
                    print self.phi(params),
                    print self.activation(self.phi(params))

                # now that we've changed its state, we need to re-update it
                self.update(params)
        
        def phi(self, params):
            """ 
            Converts current time + time offset into phi (oscillatory phase parameter in range [0,1]) 
            """
            return ((params.time + self.offset) % self.CYCLE_TIME)/self.CYCLE_TIME + 0.01
        
        def activation(self, phi):
            """ 
            Converts phi into activation level. Activation function must be concave in order for
            this algorithm to work.
            """
            return pow(phi, 1/self.EXP)
            
        def activation_to_phi(self, f):
            """ Convert from an activation level back to a phi value. """
            return pow(f, self.EXP)
            
        def update(self, params):
            """ 
            Note the time when activation crosses threshold, so we can use it as the onset time for rendering the
            actual blink. Return whether firefly has just crossed the threshold or not so we know whether to nudge its
            neighbors.
            """
            p = self.phi(params)
            blink = self.activation(p) >= 1
            if blink:
                self.blinktime = params.time
            return blink
            
        def render(self, params, frame):
            """
            Draw pulses with sinusoidal ramp-up/ramp-down
            """
            dt = params.time - self.blinktime
            dur = float(self.CYCLE_TIME)/2
            if dt < dur:
                scale = math.sin(math.pi * dt/dur)
                for v,c in enumerate(self.color):
                    frame[self.edge][v] += c * scale
    
    def __init__(self, model):
        self.cyclers = [ FireflySwarm.Firefly(e) for e in range(model.numLEDs) ]
        
    def render(self, model, params, frame):
        for c in self.cyclers:
            if c.update(params):
                # the first root node nudges all the other ones - otherwise the trees
                # won't sync with each other
                if c.edge == model.roots[0]:
                    for m in model.roots[1:]:
                        self.cyclers[m].nudge(params)
                # each firefly affects its local neighbors only. having nudges propagate
                # outward only is both prettier (synchronization starts at the brainstem
                # and moves up) and faster.
                for adj in model.outwardAdjacency[c.edge]:
                    self.cyclers[adj].nudge(params)
        for c in self.cyclers:
            c.render(params, frame)

            
class WhiteOut(EffectLayer):
    """ Sets everything to white """
    def render(self, model, params, frame):
        for i, rgb in enumerate(frame):
            mixAdd( rgb, 1, 1, 1 )    
            

class GammaLayer(EffectLayer):
    """Apply a gamma correction to the brightness, to adjust for the eye's nonlinear sensitivity."""

    def __init__(self, gamma):
        self.gamma = gamma

    def render(self, model, params, frame):
        numpy.clip(frame, 0, 1, frame)
        numpy.power(frame, self.gamma, frame)


