import asyncio
import collections
import time

import janus
import pyaudio

# Using a namedtuple for audio chunks due to their lightweight nature
AudioChunk = collections.namedtuple('AudioChunk',
                                    ['start_time', 'audio', 'width', 'freq'])


def chunk_samples(chunk):
    return len(chunk.audio) / chunk.width


def even_chunk_iterator(iterable, chunk_samples):
    sample_cnt = 0
    sample_queue = collections.deque()
    for chunk in iterable:
        while chunk is not None:
            sample_cnt += chunk_samples(chunk)
            if sample_cnt < chunk_samples:
                sample_queue.append(chunk)
                chunk = None
                continue
            elif sample_cnt == chunk_samples:
                sample_queue.append(chunk)
                yield merge_chunks(sample_queue)
                sample_queue = collections.deque()
                chunk = None
            else:
                # We need to break up the chunk
                overshoot = sample_cnt - chunk_samples
                overshoot_samples = overshoot * chunk.width
                ret_audio = buffer(chunk.audio, 0, overshoot_samples)
                leftover_audio = buffer(chunk.audio, overshoot_samples)
                leftover_chunk = AudioChunk(
                    chunk.start_time, leftover_audio, chunk.width, chunk.freq
                )
                ret_chunk = AudioChunk(
                    chunk.start_time, ret_audio, chunk.width, chunk.freq
                )
                sample_queue.append(ret_chunk)
                yield merge_chunks(sample_queue)
                sample_queue = collections.deque()
                chunk = leftover_chunk


class _ListenCtxtMgr(object):
    def __init__(self, source):
        self._source = source

    async def __aenter__(self):
        await self._source.start()

    async def __aexit__(self, *args):
        await self._source.stop()


class AudioSource(object):
    def __init__(self):
        self.running = False

    def listen(self):
        return _ListenCtxtMgr(self)

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False


class Microphone(AudioSource):
    def __init__(self,
                 audio_format=pyaudio.paInt16,
                 channels=1,
                 rate=16000,
                 device_ndx=0):
        super(Microphone, self).__init__()
        self._format = audio_format
        self._channels = channels
        self._rate = rate
        self._device_ndx = device_ndx
        self._pyaudio = None
        self._stream = None
        self._stream_queue = None

    async def start(self):
        await super(Microphone, self).start()
        loop = asyncio.get_event_loop()
        self._stream_queue = janus.Queue(loop=loop)

        self._pyaudio = pyaudio.PyAudio()
        self._stream = self._pyaudio.open(
            input=True,
            format=self._format,
            channels=self._channels,
            rate=self._rate,
            input_device_index=self._device_ndx,
            stream_callback=self._stream_callback
        )

    async def stop(self):
        await super(Microphone, self).stop()
        self._stream.close()
        self._pyaudio.terminate()

    async def get_chunk(self):
        raw_chunk = await self._stream_queue.async_q.get()
        return AudioChunk(start_time=raw_chunk[0]['input_buffer_adc_time'],
                          audio=raw_chunk[1])

    def _stream_callback(self, in_data, frame_count,
                         time_info, status_flags):
        self._stream_queue.sync_q.put((time_info, in_data))
        retflag = pyaudio.paContinue if self.running else pyaudio.paComplete
        return (None, retflag)


class SquelchedSource(AudioSource):
    def __init__(self, source, squelch_level=None):
        super(SquelchedSource, self).__init__()
        self._source = source
        self._recent_chunks = collections.deque(maxlen=100)
        self.squelch_level = squelch_level

    async def detect_squelch_level(self, detect_time=10):
        start_time = time.time()
        end_time = start_time + detect_time
        audio_chunks = collections.deque()
        async with self._source.listen():
            while time.time() < end_time:
                audio_chunks.append(await self._source.get_chunk())
        level = 1
        self.squelch_level = level
        return level
