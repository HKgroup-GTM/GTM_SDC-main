"""Split restarted streams and join consecutive selected VC3 or TMTC ranges."""

from calendar import isleap
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Optional


FRAME_SIZE = 1279
PAYLOAD_SIZE = 1105
PACKET_SIZE = 1110
TMTC_PACKET_SIZE = 144
# The current native decoder reads at most this many bytes per invocation.
MAX_DECODER_INPUT_SIZE = 1174405120
OUTPUT_SUFFIXES = ('_science_pipeline.csv', '_science_raw.txt', '_weird_science.bin',
                   '_tmtc_all.csv', '_tmtc_master.csv', '_tmtc_slave.csv')


def _sequence(packet):
    if len(packet) < 6 or packet[:2] not in (b'\x88\x55', b'\x88\xaa') or packet[4:6] != b'\x04\x4f':
        raise ValueError('invalid science packet header or length')
    return packet[3]


def _stamp(path):
    stat = Path(path).stat()
    return stat.st_size, stat.st_mtime_ns


@dataclass(frozen=True)
class VC3Info:
    path: str
    stamp: tuple
    channel: int
    first_count: int
    last_count: int
    first_offset: int
    first_sequence: int
    last_complete_sequence: Optional[int]
    leading: bytes
    tail: bytes


def inspect_vc3(path, start=0, stop=None):
    """Validate frames, FHPs and science headers without decoding event data."""
    path = str(path)
    if Path(path).suffix.lower() != '.vc3':
        raise ValueError('continuous matching supports science .vc3 files')
    stamp = _stamp(path)
    if not stamp[0] or stamp[0] % FRAME_SIZE:
        raise ValueError('incomplete VC3 transfer frame')
    stop = stamp[0] if stop is None else stop
    if not 0 <= start < stop <= stamp[0] or start % FRAME_SIZE or stop % FRAME_SIZE:
        raise ValueError('invalid VC3 frame range')
    channel = first_count = previous_count = None
    first_offset = first_sequence = last_sequence = None
    leading = pending = b''
    with open(path, 'rb') as source:
        source.seek(start)
        for index in range((stop - start) // FRAME_SIZE):
            frame = source.read(FRAME_SIZE)
            if len(frame) != FRAME_SIZE or frame[:4] != b'\x1a\xcf\xfc\x1d':
                raise ValueError('invalid VC3 synchronization marker')
            identity = int.from_bytes(frame[4:6], 'big')
            status = int.from_bytes(frame[8:10], 'big')
            # TM version 0, VC3, OCF present, no secondary header, packet data.
            if identity >> 14 or (identity & 15) != 7 or status & 0xf800 != 0x1800:
                raise ValueError('unsupported VC3 frame layout')
            if channel is None:
                channel, first_count = identity, frame[7]
            if identity != channel or (previous_count is not None and frame[7] != (previous_count + 1) % 256):
                raise ValueError('channel change or discontinuous VC frame counter')
            previous_count = frame[7]
            pointer = status & 0x7ff
            payload = frame[10:1115]
            if first_offset is None:
                if pointer == 0x7ff:
                    leading += payload
                    if len(leading) >= PACKET_SIZE:
                        raise ValueError('no science packet start within one packet length')
                    continue
                if pointer >= PAYLOAD_SIZE:
                    raise ValueError('invalid or idle first header pointer')
                first_offset = index * PAYLOAD_SIZE + pointer
                if first_offset >= PACKET_SIZE:
                    raise ValueError('invalid initial science packet offset')
                leading += payload[:pointer]
                pending = payload[pointer:]
            else:
                expected = (PACKET_SIZE - len(pending)) % PACKET_SIZE
                if pointer != (expected if expected < PAYLOAD_SIZE else 0x7ff):
                    raise ValueError('first header pointer disagrees with packet boundaries')
                pending += payload
            while len(pending) >= PACKET_SIZE:
                sequence = _sequence(pending)
                if last_sequence is not None and sequence != (last_sequence + 1) % 256:
                    raise ValueError('discontinuous science packet sequence')
                if first_sequence is None:
                    first_sequence = sequence
                last_sequence = sequence
                pending = pending[PACKET_SIZE:]
        if len(pending) >= 6:
            sequence = _sequence(pending)
            if last_sequence is not None and sequence != (last_sequence + 1) % 256:
                raise ValueError('discontinuous final science packet sequence')
            if first_sequence is None:
                first_sequence = sequence
    if first_sequence is None:
        raise ValueError('not enough science header data to identify continuity')
    if _stamp(path) != stamp:
        raise ValueError('file changed during continuity inspection')
    return VC3Info(path, stamp, channel, first_count, previous_count, first_offset,
                   first_sequence, last_sequence, leading, pending)


@dataclass(frozen=True)
class InputSegment:
    path: str
    start: int = 0
    stop: Optional[int] = None
    part: int = 0
    part_count: int = 1

    @property
    def stem(self):
        return Path(self.path).stem + (f'_part{self.part}' if self.part else '')

    @property
    def label(self):
        if self.part:
            return f'{Path(self.path).name} part{self.part} bytes [{self.start}, {self.stop})'
        return Path(self.path).name


def _is_stream_restart(previous, frame):
    """Require independent reset evidence, not a dropped frame or normal wrap."""
    if (len(frame) != FRAME_SIZE or frame[:4] != b'\x1a\xcf\xfc\x1d'
            or frame[4:6] != previous[4:6] or frame[7] != 0 or previous[7] == 255
            or frame[8:10] != b'\x18\x00'):
        return False
    previous_pointer = int.from_bytes(previous[8:10], 'big') & 0x7ff
    # An unknown/no-start pointer alone does not prove a boundary change.
    if previous_pointer >= PAYLOAD_SIZE or (previous_pointer - PAYLOAD_SIZE) % PACKET_SIZE == 0:
        return False
    payload = frame[10:1115]
    return (payload[:6] in (b'\x88\x55\x00\x00\x04\x4f', b'\x88\xaa\x00\x00\x04\x4f')
            and payload[6] == 0xca and payload[48:51] == b'\xf2\xf5\xfa')


def inspect_vc3_segments(path):
    """Return fully validated, frame-aligned ranges; never edit the source."""
    path = str(path)
    if Path(path).suffix.lower() != '.vc3':
        raise ValueError('continuous matching supports science .vc3 files')
    stamp = _stamp(path)
    if not stamp[0] or stamp[0] % FRAME_SIZE:
        raise ValueError('incomplete VC3 transfer frame')
    boundaries = [0]
    with open(path, 'rb') as source:
        previous = source.read(FRAME_SIZE)
        for offset in range(FRAME_SIZE, stamp[0], FRAME_SIZE):
            frame = source.read(FRAME_SIZE)
            if len(previous) == FRAME_SIZE and _is_stream_restart(previous, frame):
                boundaries.append(offset)
            previous = frame
    boundaries.append(stamp[0])
    count = len(boundaries) - 1
    result = []
    for index, (start, stop) in enumerate(zip(boundaries, boundaries[1:])):
        # A reset must also be followed by enough consistent frames to confirm it.
        if index and (stop - start) < 3 * FRAME_SIZE:
            raise ValueError('not enough following frames to confirm a stream restart')
        info = inspect_vc3(path, start, stop)
        segment = InputSegment(path, start, stop, index + 1 if count > 1 else 0, count)
        result.append((segment, info))
    if _stamp(path) != stamp:
        raise ValueError('file changed during stream inspection')
    return result


def can_connect(left, right):
    if left.channel != right.channel or right.first_count != (left.last_count + 1) % 256:
        return False
    # Known timestamp filenames constrain ordering; counters alone wrap around.
    names = [re.fullmatch(r'(.+?)_(\d{13})', Path(info.path).stem) for info in (left, right)]
    if all(names) and names[0][1] == names[1][1] and names[0][2] >= names[1][2]:
        return False
    if right.first_offset != (-len(left.tail)) % PACKET_SIZE:
        return False
    last_sequence = left.last_complete_sequence
    if left.tail:
        crossing = left.tail + right.leading
        if len(crossing) != PACKET_SIZE:
            return False
        try:
            crossing_sequence = _sequence(crossing)
        except ValueError:
            return False
        if last_sequence is not None and crossing_sequence != (last_sequence + 1) % 256:
            return False
        last_sequence = crossing_sequence
    return last_sequence is not None and right.first_sequence == (last_sequence + 1) % 256


@dataclass(frozen=True)
class TMTCRecord:
    sequence: int
    counter: int
    utc: int
    startup: bool


def _tmtc_record(packet):
    # The same 6 + 10 + 128-byte layout used by parse_tmtc_data in the DLL.
    if (len(packet) != TMTC_PACKET_SIZE or packet[:2] != b'\x08\x91'
            or packet[2] >> 6 != 3 or packet[4:6] != b'\x00\x89'
            or packet[16:18] != b'\x55\xaa' or packet[18] not in (2, 5)
            or packet[21:23] != b'\x00\x78' or packet[-2:] != b'\xfb\xf2'):
        raise ValueError('invalid TMTC GICD/ICD header, length, module or tail')
    year = int.from_bytes(packet[6:8], 'big')
    day = int.from_bytes(packet[8:10], 'big')
    hour, minute, second, subsecond = packet[10:14]
    if (not 1 <= year <= 9999 or not 1 <= day <= 365 + isleap(year)
            or hour >= 24 or minute >= 60 or second >= 60):
        raise ValueError('invalid TMTC GICD timestamp')
    utc = (((date(year, 1, 1).toordinal() + day - 1) * 86400
            + hour * 3600 + minute * 60 + second) * 256 + subsecond)
    sequence = int.from_bytes(packet[2:4], 'big') & 0x3fff
    counter = int.from_bytes(packet[19:21], 'big')
    # Observed startup signature: both counters restart and ICD time is unset.
    startup = sequence == 1 and counter == 3 and packet[23:31] == bytes(8)
    return TMTCRecord(sequence, counter, utc, startup)


def _tmtc_follows(left, right):
    return (right.sequence == (left.sequence + 1) % 16384
            and right.counter == (left.counter + 1) % 65536 and right.utc > left.utc)


@dataclass(frozen=True)
class TMTCInfo:
    path: str
    stamp: tuple
    first: TMTCRecord
    last: TMTCRecord
    first_step: int
    last_step: int
    leading: bytes
    tail: bytes


def inspect_tmtc_segments(path):
    """Validate space TMTC packets, including partial file edges and known resets."""
    path = str(path)
    if Path(path).suffix.lower() != '.dmp':
        raise ValueError('continuous TMTC matching supports in-space .dmp files')
    stamp = _stamp(path)
    ranges = []
    with open(path, 'rb') as source:
        prefix = source.read(2 * TMTC_PACKET_SIZE - 1)
        offsets = []
        for offset in range(min(TMTC_PACKET_SIZE, len(prefix))):
            try:
                _tmtc_record(prefix[offset:offset + TMTC_PACKET_SIZE])
                offsets.append(offset)
            except ValueError:
                pass
        if len(offsets) != 1:
            raise ValueError('no unique TMTC packet alignment within the first 144 bytes')
        offset = offsets[0]
        leading = prefix[:offset]
        source.seek(offset)
        start = 0
        first = previous = None
        first_step = last_step = 0
        count = 0

        def finish(stop, tail=b''):
            if count < (3 if ranges else 2):
                raise ValueError('not enough TMTC packets to confirm continuity or restart')
            ranges.append((start, stop, TMTCInfo(path, stamp, first, previous,
                                               first_step, last_step, leading, tail)))

        while True:
            packet = source.read(TMTC_PACKET_SIZE)
            if len(packet) < TMTC_PACKET_SIZE:
                finish(stamp[0], packet)
                break
            record = _tmtc_record(packet)
            if previous is not None and not _tmtc_follows(previous, record):
                if not record.startup or record.utc <= previous.utc:
                    raise ValueError('discontinuous TMTC sequence, packet counter or timestamp')
                finish(offset)
                start, leading = offset, b''
                first = previous = None
                first_step = last_step = count = 0
            if previous is not None:
                last_step = record.utc - previous.utc
                if count == 1:
                    first_step = last_step
            if first is None:
                first = record
            previous = record
            count += 1
            offset += TMTC_PACKET_SIZE
    if _stamp(path) != stamp:
        raise ValueError('file changed during TMTC inspection')
    return [(InputSegment(path, start, stop, index + 1 if len(ranges) > 1 else 0, len(ranges)), info)
            for index, (start, stop, info) in enumerate(ranges)]


def can_connect_tmtc(left, right):
    # APID 145 does not identify the spacecraft; retain known filename identity too.
    names = [re.fullmatch(r'(.+?)_(\d{13})_(\d+)', Path(info.path).stem) for info in (left, right)]
    if all(names) and (names[0][1] != names[1][1] or names[0][3] != names[1][3]
                       or names[0][2] >= names[1][2]):
        return False
    if len(right.leading) != (-len(left.tail)) % TMTC_PACKET_SIZE:
        return False
    records = [left.last]
    if left.tail:
        try:
            records.append(_tmtc_record(left.tail + right.leading))
        except ValueError:
            return False
    records.append(right.first)
    # Match the cadence on both sides, rather than guessing from filenames or
    # counters alone (both counters can wrap between unrelated observations).
    return all(_tmtc_follows(a, b) and b.utc - a.utc == left.last_step == right.first_step
               for a, b in zip(records, records[1:]))


def group_selected_files(paths, report=print, decode_mode=2):
    """Group validated segments; keep unknown files and ambiguous ranges separate."""
    paths = list(dict.fromkeys(map(str, paths)))
    infos, segments = {}, []
    for path in paths:
        try:
            inspected = inspect_tmtc_segments(path) if decode_mode == 1 else inspect_vc3_segments(path)
            for segment, info in inspected:
                segments.append(segment)
                infos[segment] = info
                if segment.part > 1:
                    if decode_mode == 1:
                        report(f'Continuous Files: TMTC stream restart in {Path(path).name}: '
                               f'byte offset {segment.start}; source sequence and GTM counter reset, '
                               'unset ICD time and consistent following packets')
                    else:
                        report(f'Continuous Files: stream restart in {Path(path).name}: '
                               f'byte offset {segment.start}, frame {segment.start // FRAME_SIZE + 1}; '
                               'VC counter reset, new FHP alignment, sequence 0 and valid sync record')
        except (OSError, ValueError) as error:
            report(f'Continuous Files: decode individually: {Path(path).name} ({error})')
            segments.append(InputSegment(path))

    def follows(left, right):
        # An identified restart is a hard boundary, even if counters later alias.
        if left.path == right.path or right.part > 1 or 0 < left.part < left.part_count:
            return False
        match = can_connect_tmtc if decode_mode == 1 else can_connect
        return match(infos[left], infos[right])

    successors = {segment: [other for other in infos if follows(segment, other)] for segment in infos}
    predecessors = {segment: [other for other, candidates in successors.items() if segment in candidates]
                    for segment in infos}
    links = {segment: candidates[0] for segment, candidates in successors.items()
             if len(candidates) == 1 and len(predecessors[candidates[0]]) == 1}
    groups, visited = [], set()
    for segment in segments:
        if segment in links.values():
            continue
        chain = []
        while segment not in visited:
            visited.add(segment)
            chain.append(segment)
            if segment not in links:
                break
            segment = links[segment]
        groups.append(chain)
    # Cycles cannot be ordered reliably, so leave every member independent.
    groups.extend([segment] for segment in segments if segment not in visited)
    groups.sort(key=lambda group: min(segments.index(segment) for segment in group))
    return groups, infos


@dataclass(frozen=True)
class DecodeInput:
    sources: tuple
    input_path: str
    output_prefix: str

    @property
    def merged(self):
        # A single extracted part is also a fixed temporary input, not a live file.
        return self.input_path != self.output_prefix

    def publish_outputs(self):
        if self.merged:
            for suffix in OUTPUT_SUFFIXES:
                source = Path(self.input_path + suffix)
                if source.exists():
                    shutil.copyfile(source, self.output_prefix + suffix)


@contextmanager
def prepare_decode_inputs(paths, enabled, in_space_flag, decode_mode, report=print):
    paths = list(map(str, paths))
    with ExitStack() as stack:
        if not enabled or in_space_flag != 1 or decode_mode not in (1, 2):
            yield [DecodeInput((path,), path, path) for path in paths]
            return
        groups, infos = group_selected_files(paths, report, decode_mode)

        def combined_output(group):
            suffix = '.dmp' if decode_mode == 1 else '.vc3'
            return str(Path(group[0].path).parent / (''.join(segment.stem for segment in group) + suffix))

        def needs_staging(group):
            return len(group) > 1 or any(segment.part for segment in group)

        def path_key(path):
            return os.path.normcase(os.path.abspath(path))

        selected_keys = {path_key(path) for path in paths}
        combined_keys = [path_key(combined_output(group)) for group in groups if needs_staging(group)]
        jobs = []
        failed_paths = set()
        for group in groups:
            if needs_staging(group):
                try:
                    output = combined_output(group)
                    if path_key(output) in selected_keys or combined_keys.count(path_key(output)) > 1:
                        raise ValueError('combined output name conflicts with another selected input or group')
                    if sum(segment.stop - segment.start for segment in group) > MAX_DECODER_INPUT_SIZE:
                        raise ValueError('combined input exceeds the native decoder read limit')
                    filename = Path(output).name
                    temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='gtm-continuous-')))
                    merged = temporary / filename
                    with merged.open('wb') as destination:
                        for segment in group:
                            if _stamp(segment.path) != infos[segment].stamp:
                                raise ValueError(f'file changed since inspection: {segment.path}')
                            with open(segment.path, 'rb') as source:
                                source.seek(segment.start)
                                remaining = segment.stop - segment.start
                                while remaining:
                                    block = source.read(min(remaining, 1024 * 1024))
                                    if not block:
                                        raise ValueError(f'file shortened while copying: {segment.path}')
                                    destination.write(block)
                                    remaining -= len(block)
                            if _stamp(segment.path) != infos[segment].stamp:
                                raise ValueError(f'file changed while merging: {segment.path}')
                    jobs.append(DecodeInput(tuple(segment.path for segment in group), str(merged), output))
                    report('Continuous Files: prepare ' + ' -> '.join(segment.label for segment in group))
                    report(f'Continuous Files: output prefix: {output}')
                    tail_size = len(infos[group[-1]].tail)
                    if tail_size:
                        kind = 'TMTC' if decode_mode == 1 else 'science'
                        report(f'Continuous Files: {Path(output).name}: {tail_size} trailing {kind} bytes '
                               'cannot form a complete packet; kept in original input, not decoded')
                    continue
                except (OSError, ValueError) as error:
                    report(f'Continuous Files: merge unavailable; decode individually ({error})')
            else:
                report(f'Continuous Files: no unique continuation; decode individually: {group[0].label}')
            failed_paths.update(segment.path for segment in group)
        # If staging fails for any part, fall back once for every affected source.
        # Expand through staged groups so no source is decoded both whole and in part.
        while True:
            affected = {path for job in jobs if any(path in failed_paths for path in job.sources)
                        for path in job.sources}
            if affected <= failed_paths:
                break
            failed_paths.update(affected)
        jobs = [job for job in jobs if not any(path in failed_paths for path in job.sources)]
        jobs.extend(DecodeInput((path,), path, path) for path in dict.fromkeys(paths) if path in failed_paths)
        order = {path: index for index, path in enumerate(paths)}
        jobs.sort(key=lambda job: min(order[path] for path in job.sources))
        yield jobs
