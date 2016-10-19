
import asyncio
import struct

import streams
import vm


class Scope(vm.VirtualMachine):

    PARAMS_MAGIC = 0xb0b2

    @classmethod
    async def connect(cls, stream=None):
        scope = cls(stream if stream is not None else streams.SerialStream())
        await scope.setup()
        return scope

    def __init__(self, stream):
        super(Scope, self).__init__(stream)

    @staticmethod
    def _analog_map_func(ks, low, high):
         return ks[0] + ks[1]*low + ks[2]*high

    async def setup(self):
        await self.reset()
        await self.issue_get_revision()
        revision = ((await self.read_replies(2))[1]).decode('ascii')
        if revision.startswith('BS0005'):
            self.awg_clock_period = 25e-9
            self.awg_wavetable_size = 1024
            self.awg_sample_buffer_size = 1024
            self.awg_minimum_clock = 33
            self.awg_maximum_voltage = 3.3
            self.analog_low_ks = (0.43307040504672523, 0.060970272170312846, -0.0037186072558476487)
            self.analog_high_ks = (0.37575241029061407, -0.0039308497942329686, 0.060955881466731247)
            self.analog_min = -5.7
            self.analog_max = 8
            self.capture_clock_period = 25e-9
            self.capture_buffer_size = 12*1024
            self.trigger_timeout_tick = 6.4e-6
            self.trigger_low = -7.517
            self.trigger_high = 10.816
        await self.load_params()

    async def load_params(self):
        params = []
        for i in range(struct.calcsize('<H3f3f')):
            params.append(await self.read_eeprom(i+100))
        params = struct.unpack('<H3f3f', bytes(params))
        if params[0] == self.PARAMS_MAGIC:
            print("Loading params", params[1:])
            self.analog_low_ks = params[1:4]
            self.analog_high_ks = params[4:7]

    async def save_params(self):
        params = struct.pack('<H3f3f', self.PARAMS_MAGIC, *(self.analog_low_ks + self.analog_high_ks))
        for i, byte in enumerate(params):
            await self.write_eeprom(i+100, byte)

    async def capture(self, channels=['A'], trigger_channel=None, trigger_level=0, trigger_type='rising',
                            period=1e-3, nsamples=1000, timeout=None, low=None, high=None, raw=False):
        if 'A' in channels and 'B' in channels:
            nsamples_multiplier = 2
        else:
            nsamples_multiplier = 1
        ticks = int(period / nsamples / nsamples_multiplier / self.capture_clock_period)
        if ticks >= 40 and ticks < 65536:
            sample_width = 2
            buffer_width = 6*1024
            dump_mode = vm.DumpMode.Native
            if 'A' in channels and 'B' in channels:
                trace_mode = vm.TraceMode.MacroChop
                buffer_mode = vm.BufferMode.MacroChop
            else:
                trace_mode = vm.TraceMode.Macro
                buffer_mode = vm.BufferMode.Macro
        elif ticks >= 15 and ticks < 40:
            sample_width = 1
            buffer_width = 12*1024
            dump_mode = vm.DumpMode.Raw
            if 'A' in channels and 'B' in channels:
                trace_mode = vm.TraceMode.AnalogChop
                buffer_mode = vm.BufferMode.Chop
            else:
                trace_mode = vm.TraceMode.Analog
                buffer_mode = vm.BufferMode.Single
        elif ticks >= 8 and ticks < 15:
            sample_width = 1
            buffer_width = 12*1024
            dump_mode = vm.DumpMode.Raw
            if 'A' in channels and 'B' in channels:
                trace_mode = vm.TraceMode.AnalogFastChop
                buffer_mode = vm.BufferMode.Chop
            else:
                trace_mode = vm.TraceMode.AnalogFast
                buffer_mode = vm.BufferMode.Single
        elif ticks >= 2 and ticks < 8:
            if ticks > 5:
                ticks = 5
            sample_width = 1
            buffer_width = 12*1024
            dump_mode = vm.DumpMode.Raw
            if 'A' in channels and 'B' in channels:
                trace_mode = vm.TraceMode.AnalogShotChop
                buffer_mode = vm.BufferMode.Chop
            else:
                trace_mode = vm.TraceMode.AnalogShot
                buffer_mode = vm.BufferMode.Single
        else:
            raise RuntimeError("Unsupported clock period: {}".format(ticks))
        nsamples = int(round(period / ticks / nsamples_multiplier / self.capture_clock_period))
        total_samples = nsamples * nsamples_multiplier
        assert total_samples <= buffer_width

        if low is None:
            low = 0 if raw else self.analog_min
        if high is None:
            high = 1 if raw else self.analog_max

        if trigger_channel is None:
            trigger_channel = channels[0]
        else:
            assert trigger_channel in channels
        spock_option = vm.SpockOption.TriggerTypeHardwareComparator
        if trigger_channel == 'A':
            kitchen_sink_a = vm.KitchenSinkA.ChannelAComparatorEnable
            spock_option |= vm.SpockOption.TriggerSourceA
        elif trigger_channel == 'B':
            kitchen_sink_a = vm.KitchenSinkA.ChannelBComparatorEnable
            spock_option |= vm.SpockOption.TriggerSourceB
        if trigger_type.lower() in {'falling', 'below'}:
            spock_option |= vm.SpockOption.TriggerInvert
        trigger_intro = 0 if trigger_type.lower() in {'above', 'below'} else 4
        if not raw:
            trigger_level = (trigger_level - self.trigger_low) / (self.trigger_high - self.trigger_low)
        analog_enable = 0
        if 'A' in channels:
            analog_enable |= 1
        if 'B' in channels:
            analog_enable |= 2

        async with self.transaction():
            await self.set_registers(TraceMode=trace_mode, ClockTicks=ticks, ClockScale=1,
                                     TraceIntro=total_samples//2, TraceOutro=total_samples//2, TraceDelay=0,
                                     Timeout=int(round((period*5 if timeout is None else timeout) / self.trigger_timeout_tick)),
                                     TriggerMask=0x7f, TriggerLogic=0x80, TriggerLevel=trigger_level, 
                                     TriggerIntro=trigger_intro, TriggerOutro=4, SpockOption=spock_option, Prelude=0,
                                     ConverterLo=low if raw else self._analog_map_func(self.analog_low_ks, low, high),
                                     ConverterHi=high if raw else self._analog_map_func(self.analog_high_ks, low, high),
                                     KitchenSinkA=kitchen_sink_a,
                                     KitchenSinkB=vm.KitchenSinkB.AnalogFilterEnable | vm.KitchenSinkB.WaveformGeneratorEnable,
                                     AnalogEnable=analog_enable, BufferMode=buffer_mode, SampleAddress=0)
            await self.issue_program_spock_registers()
            await self.issue_configure_device_hardware()
            await self.issue_triggered_trace()
        while True:
            code, timestamp = await self.read_replies(2)
            code = int(code.decode('ascii'), 16)
            timestamp = int(timestamp.decode('ascii'), 16)
            if code == 2:
                start_timestamp = timestamp
            else:
                end_timestamp = timestamp
                break
        address = int((await self.read_replies(1))[0].decode('ascii'), 16) // nsamples_multiplier
        traces = {}
        for dump_channel, channel in enumerate(sorted(channels)):
            async with self.transaction():
                await self.set_registers(SampleAddress=(address - nsamples) * nsamples_multiplier % buffer_width, 
                                         DumpMode=dump_mode, DumpChan=dump_channel,
                                         DumpCount=nsamples, DumpRepeat=1, DumpSend=1, DumpSkip=0)
                await self.issue_program_spock_registers()
                await self.issue_analog_dump_binary()
            data = await self._stream.readexactly(nsamples * sample_width)
            if sample_width == 2:
                if raw:
                    trace = [(value / 65536 + 0.5) for value in struct.unpack('>{}h'.format(nsamples), data)]
                else:
                    trace = [(value / 65536 + 0.5) * (high - low) + low for value in struct.unpack('>{}h'.format(nsamples), data)]
            else:
                if raw:
                    trace = [value / 256 for value in data]
                else:
                    trace = [value / 256 * (high - low) + low for value in data]
            traces[channel] = trace
        return traces

    async def start_generator(self, frequency, waveform='sine', wavetable=None, ratio=0.5, vpp=None, offset=0,
                                    min_samples=50, max_error=1e-4):
        if vpp is None:
            vpp = self.awg_maximum_voltage
        possible_params = []
        max_clock = int(round(1 / frequency / min_samples / self.awg_clock_period, 0))
        for clock in range(self.awg_minimum_clock, max_clock+1):
            width = 1 / frequency / (clock * self.awg_clock_period)
            if width <= self.awg_sample_buffer_size:
                nwaves = int(self.awg_sample_buffer_size / width)
                size = int(round(nwaves * width))
                width = size / nwaves
                actualf = 1 / (width * clock * self.awg_clock_period)
                error = abs(frequency - actualf) / frequency
                if error < max_error:
                    possible_params.append(((error == 0, width), (size, nwaves, clock, actualf)))
            clock += 1
        if not possible_params:
            raise ValueError("No solution to required frequency/min_samples/max_error")
        size, nwaves, clock, actualf = sorted(possible_params)[-1][1]
        async with self.transaction():
            if wavetable is None:
                mode = {'sine': 0, 'triangle': 1, 'sawtooth': 1, 'exponential': 2, 'square': 3}[waveform.lower()]
                await self.set_registers(Cmd=0, Mode=mode, Ratio=ratio)
                await self.issue_synthesize_wavetable()
            else:
                if len(wavetable) != self.awg_wavetable_size:
                    raise ValueError("Wavetable data must be {} samples".format(self.awg_wavetable_size))
                await self.set_registers(Cmd=0, Mode=1, Address=0, Size=1)
                await self.wavetable_write_bytes(wavetable)
            await self.set_registers(Cmd=0, Mode=0, Level=vpp/self.awg_maximum_voltage,
                                     Offset=2*offset/self.awg_maximum_voltage,
                                     Ratio=nwaves * self.awg_wavetable_size / size,
                                     Index=0, Address=0, Size=size)
            await self.issue_translate_wavetable()
            await self.set_registers(Cmd=2, Mode=0, Clock=clock, Modulo=size, 
                                     Mark=10, Space=1, Rest=0x7f00, Option=0x8004)
            await self.issue_control_waveform_generator()
            await self.set_registers(KitchenSinkB=vm.KitchenSinkB.WaveformGeneratorEnable)
            await self.issue_configure_device_hardware()
            await self.issue('.')
        return actualf

    async def stop_generator(self):
        async with self.transaction():
            await self.set_registers(Cmd=1, Mode=0)
            await self.issue_control_waveform_generator()
            await self.set_registers(KitchenSinkB=0)
            await self.issue_configure_device_hardware()

    async def read_wavetable(self):
        with self.transaction():
            self.set_registers(Address=0, Size=self.awg_wavetable_size)
            self.issue_wavetable_read()
        return list(self.read_exactly(self.awg_wavetable_size))

    async def read_eeprom(self, address):
        async with self.transaction():
            await self.set_registers(EepromAddress=address)
            await self.issue_read_eeprom()
        return int((await self.read_replies(2))[1], 16)

    async def write_eeprom(self, address, byte):
        async with self.transaction():
            await self.set_registers(EepromAddress=address, EepromData=byte)
            await self.issue_write_eeprom()
        return int((await self.read_replies(2))[1], 16)

    async def calibrate(self, channels='AB', n=40):
        import numpy as np
        import pandas as pd
        from scipy.optimize import leastsq
        await self.start_generator(1000, waveform='square')
        items = []
        for low in np.linspace(0.063, 0.4, n):
            for high in np.linspace(0.877, 0.6, n):
                data = await self.capture(channels=channels, period=1e-3, trigger_level=0.5, nsamples=1000, low=low, high=high, raw=True)
                values = np.hstack(list(data.values()))
                values.sort()
                zero = values[10:len(values)//2-10].mean()
                v33 = values[-len(values)//2+10:-10].mean()
                analog_range = 3.3 / (v33 - zero)
                analog_low = -zero * analog_range
                analog_high = analog_low + analog_range
                items.append({'low': low, 'high': high, 'analog_low': analog_low, 'analog_high': analog_high})
        data = pd.DataFrame(items)
        analog_low_ks, success1 = leastsq(lambda ks, low, high, y: y - self._analog_map_func(ks, low, high), self.analog_low_ks,
                                          args=(data.analog_low, data.analog_high, data.low))
        if success1:
            self.analog_low_ks = tuple(analog_low_ks)
        analog_high_ks, success2 = leastsq(lambda ks, low, high, y: y - self._analog_map_func(ks, low, high), self.analog_high_ks,
                                           args=(data.analog_low, data.analog_high, data.high))
        if success2:
            self.analog_high_ks = tuple(analog_high_ks)
        await self.stop_generator()
        return success1 and success2


import numpy as np
import pandas as pd

async def main():
    global s, x, y, data
    s = await Scope.connect()
    x = np.linspace(0, 2*np.pi, s.awg_wavetable_size, endpoint=False)
    y = np.round((np.sin(x)**5)*127 + 128, 0).astype('uint8')
    await s.start_generator(1000, wavetable=y)
    #if await s.calibrate():
    #    await s.save_params()

def capture(*args, **kwargs):
    return pd.DataFrame(asyncio.get_event_loop().run_until_complete(s.capture(*args, **kwargs)))

if __name__ == '__main__':
    asyncio.get_event_loop().run_until_complete(main())

