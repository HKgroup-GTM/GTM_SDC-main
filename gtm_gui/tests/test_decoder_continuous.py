"""Exercise selection-only VC3 matching, staging, and decoder integration."""

from contextlib import redirect_stdout
from io import StringIO
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt5 import QtWidgets
from GTM_Decoder_Continuous import group_selected_files, inspect_vc3, inspect_vc3_segments, prepare_decode_inputs
from GTM_SDC_UI import Ui_MainWindow
from GTM_SDC_UI_Controller_Decoder_Thread import UiDecoderThread
from GTM_SDC_UI_Controller_Decoder_Thread_C import c_decoder, DecoderError


def science_frames(count, first_count=250, first_sequence=250, startup_sync=False):
    packets = b''.join(bytes((0x88, 0x55, 0, (first_sequence + i) % 256, 4, 0x4f))
                       + b'\x80\x00\x00' * 368 for i in range(count + 1))
    if startup_sync:
        packets = packets[:6] + b'\xca' + bytes(41) + b'\xf2\xf5\xfa' + packets[51:]
    frames = []
    for i in range(count):
        pointer = (-i * 1105) % 1110
        if pointer >= 1105:
            pointer = 0x7ff
        header = b'\x08\x27\x00' + bytes(((first_count + i) % 256,))
        header += (0x1800 | pointer).to_bytes(2, 'big')
        frames.append(b'\x1a\xcf\xfc\x1d' + header + packets[i*1105:(i+1)*1105] + bytes(164))
    return frames


def csv_digest(path):
    result = hashlib.sha256()
    rows = 0
    with path.open('rb') as csv:
        for block in iter(lambda: csv.read(1024 * 1024), b''):
            result.update(block)
            rows += block.count(b'\n')
    return result.hexdigest(), rows


class ContinuousFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        frames = science_frames(20)
        self.a = self.write('A.vc3', b''.join(frames[:7]))
        self.b = self.write('B.vc3', b''.join(frames[7:13]))
        self.c = self.write('C.vc3', b''.join(frames[13:]))
        self.messages = []

    def write(self, name, data):
        path = self.folder / name
        path.write_bytes(data)
        return str(path)

    def groups(self, paths):
        return [[segment.path for segment in group]
                for group in group_selected_files(paths, self.messages.append)[0]]

    def prepare(self, paths, enabled=True, in_space=1, mode=2):
        return prepare_decode_inputs(paths, enabled, in_space, mode, self.messages.append)

    def test_reversed_selection_and_counter_wrap(self):
        self.assertEqual(inspect_vc3(self.a).last_count, 0)
        self.assertEqual(self.groups([self.c, self.a, self.b]), [[self.a, self.b, self.c]])

    def restart_selection(self):
        first = science_frames(20, 0, 0, startup_sync=True)
        second = science_frames(16, 0, 0, startup_sync=True)
        a = self.write('FS8B_2026232013029.vc3', b''.join(first[:8]))
        b = self.write('FS8B_2026232023029.vc3', b''.join(first[8:] + second[:7]))
        c = self.write('FS8B_2026232033030.vc3', b''.join(second[7:]))
        return a, b, c, 12 * 1279

    def test_internal_restart_produces_two_exact_groups_and_names(self):
        a, b, c, cut = self.restart_selection()
        original = {path: Path(path).read_bytes() for path in (a, b, c)}
        parts = inspect_vc3_segments(b)
        self.assertEqual([(part.start, part.stop, part.part) for part, _ in parts],
                         [(0, cut, 1), (cut, len(original[b]), 2)])
        names = [Path(a).stem + Path(b).stem + '_part1.vc3',
                 Path(b).stem + '_part2' + Path(c).stem + '.vc3']
        expected = {names[0]: original[a] + original[b][:cut],
                    names[1]: original[b][cut:] + original[c]}
        with self.prepare([c, b, a]) as jobs:
            self.assertEqual(len(jobs), 2)
            self.assertEqual({Path(job.output_prefix).name for job in jobs}, set(expected))
            for job in jobs:
                self.assertEqual(Path(job.input_path).read_bytes(), expected[Path(job.output_prefix).name])
        self.assertEqual({path: Path(path).read_bytes() for path in original}, original)
        self.assertTrue(any(f'byte offset {cut}' in message for message in self.messages))
        self.assertTrue(any('trailing science bytes' in message for message in self.messages))

    def test_one_selected_file_can_split_and_publish_both_parts(self):
        _, b, _, cut = self.restart_selection()
        original = Path(b).read_bytes()
        with self.prepare([b]) as jobs:
            self.assertEqual([Path(job.output_prefix).name for job in jobs],
                             [Path(b).stem + '_part1.vc3', Path(b).stem + '_part2.vc3'])
            self.assertTrue(all(job.merged for job in jobs))
            self.assertEqual([Path(job.input_path).read_bytes() for job in jobs],
                             [original[:cut], original[cut:]])
            for index, job in enumerate(jobs):
                Path(job.input_path + '_science_pipeline.csv').write_text(f'part{index + 1}')
                job.publish_outputs()
                self.assertEqual(Path(job.output_prefix + '_science_pipeline.csv').read_text(), f'part{index + 1}')

    def test_multiple_restarts_in_one_file(self):
        data = b''.join(science_frames(8, 0, 0, True))
        path = self.write('three_streams.vc3', data * 3)
        with self.prepare([path]) as jobs:
            self.assertEqual(len(jobs), 3)
            self.assertEqual([Path(job.output_prefix).name for job in jobs],
                             [f'three_streams_part{i}.vc3' for i in (1, 2, 3)])
            self.assertTrue(all(Path(job.input_path).read_bytes() == data for job in jobs))

    def test_counter_gap_alone_and_reset_without_sync_do_not_split(self):
        a, b, c, cut = self.restart_selection()
        original = Path(b).read_bytes()
        for change in ('missing_sync', 'bad_sequence', 'bad_fhp', 'too_short', 'broken_following_counter'):
            damaged = bytearray(original)
            if change == 'missing_sync':
                damaged[cut + 16] = 0
            elif change == 'bad_sequence':
                damaged[cut + 13] = 1
            elif change == 'bad_fhp':
                damaged[cut + 9] = 5
            elif change == 'too_short':
                damaged = damaged[:cut + 2 * 1279]
            else:
                damaged[cut + 1279 + 7] = 30
            with self.subTest(change=change):
                Path(b).write_bytes(damaged)
                with self.prepare([b]) as jobs:
                    self.assertEqual([job.input_path for job in jobs], [b])
        Path(b).write_bytes(original)

    def test_realistic_255_to_zero_wrap_is_not_a_restart(self):
        # FHP and sync may coincide with zero; an ordinary VCFC wrap is not a reset.
        first = science_frames(256, 0, 0)
        next_stream = science_frames(4, 0, 0, True)
        path = self.write('wrap_with_wrong_packet.vc3', b''.join(first + next_stream))
        with self.prepare([path]) as jobs:
            self.assertEqual([job.input_path for job in jobs], [path])
        normal = self.write('normal_wrap.vc3', b''.join(science_frames(260, 0, 0)))
        self.assertEqual(len(inspect_vc3_segments(normal)), 1)

    def test_known_restart_is_not_rejoined_to_a_counter_alias(self):
        # A reset at the split; an unrelated selected file ends at 255 and
        # could otherwise look like a predecessor of the fresh sequence-0 stream.
        _, b, _, _ = self.restart_selection()
        alias = self.write('alias.vc3', b''.join(science_frames(222, 34, 35)))
        groups, infos = group_selected_files([b, alias], self.messages.append)
        restarted = [segment for segment in infos if segment.path == b and segment.part == 2][0]
        self.assertTrue(any(group == [restarted] for group in groups))

    def test_failed_staging_of_one_split_group_falls_back_once_per_original(self):
        a, b, c, _ = self.restart_selection()
        factory = tempfile.TemporaryDirectory
        calls = 0

        def stage(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('full disk')
            return factory(*args, **kwargs)

        with patch('GTM_Decoder_Continuous.tempfile.TemporaryDirectory', side_effect=stage):
            with self.prepare([a, b, c]) as jobs:
                self.assertEqual([job.input_path for job in jobs], [a, b, c])
                self.assertTrue(all(not job.merged for job in jobs))

    def test_checkbox_off_keeps_unsplit_inputs(self):
        a, b, c, _ = self.restart_selection()
        with self.prepare([a, b, c], enabled=False) as jobs:
            self.assertEqual([job.input_path for job in jobs], [a, b, c])

    def test_unselected_middle_is_never_added(self):
        self.assertEqual(self.groups([self.c, self.a]), [[self.c], [self.a]])

    def test_multiple_groups_and_unmatched_selection(self):
        frames = science_frames(10, 100, 80)
        d = self.write('D.vc3', b''.join(frames[:5]))
        e = self.write('E.vc3', b''.join(frames[5:]))
        invalid = self.write('invalid.vc3', b'not VC3')
        self.assertEqual(self.groups([e, self.b, invalid, self.a, d]),
                         [[d, e], [self.a, self.b], [invalid]])

    def test_ambiguous_successors_remain_independent(self):
        duplicate = self.write('B_copy.vc3', Path(self.b).read_bytes())
        self.assertEqual(self.groups([self.a, self.b, duplicate]),
                         [[self.a], [self.b], [duplicate]])

    def test_invalid_marker_fhp_counter_and_header_fall_back(self):
        original = Path(self.b).read_bytes()
        for position in (0, 9, 1279 + 7, 10 + 35):
            with self.subTest(position=position):
                damaged = bytearray(original)
                damaged[position] ^= 1
                Path(self.b).write_bytes(damaged)
                self.assertEqual(self.groups([self.a, self.b]), [[self.a], [self.b]])
        Path(self.b).write_bytes(original)

    def test_matching_frame_count_but_wrong_science_sequence_is_rejected(self):
        other = science_frames(20, 250, 20)
        Path(self.b).write_bytes(b''.join(other[7:13]))
        self.assertEqual(self.groups([self.a, self.b]), [[self.a], [self.b]])

    def test_different_spacecraft_is_not_connected(self):
        changed = bytearray(Path(self.b).read_bytes())
        for index in range(0, len(changed), 1279):
            changed[index + 4] ^= 1
        Path(self.b).write_bytes(changed)
        self.assertEqual(self.groups([self.a, self.b]), [[self.a], [self.b]])

    def test_fhp_no_new_packet_and_header_split_across_frames(self):
        frames = science_frames(224)
        a = self.write('longA.vc3', b''.join(frames[:221]))
        b = self.write('longB.vc3', b''.join(frames[221:]))
        self.assertEqual(len(inspect_vc3(a).tail), 5)
        self.assertEqual(inspect_vc3(b).first_offset, 1105)
        self.assertEqual(self.groups([b, a]), [[a, b]])

    def test_combined_bytes_naming_publish_and_temporary_cleanup(self):
        target = self.folder / 'AB.vc3'
        target.write_bytes(b'existing manually joined input must be preserved')
        with self.prepare([self.b, self.a]) as jobs:
            self.assertEqual(len(jobs), 1)
            job = jobs[0]
            self.assertEqual(job.sources, (self.a, self.b))
            self.assertEqual(job.output_prefix, str(target))
            self.assertEqual(Path(job.input_path).read_bytes(),
                             Path(self.a).read_bytes() + Path(self.b).read_bytes())
            Path(job.input_path + '_science_pipeline.csv').write_text('header\nvalue\n')
            job.publish_outputs()
            self.assertEqual(Path(str(target) + '_science_pipeline.csv').read_text(), 'header\nvalue\n')
            temporary = Path(job.input_path).parent
        self.assertFalse(temporary.exists())
        self.assertEqual(target.read_bytes(), b'existing manually joined input must be preserved')

    def test_temporary_cleanup_on_decode_error(self):
        with self.assertRaises(RuntimeError):
            with self.prepare([self.a, self.b]) as jobs:
                temporary = Path(jobs[0].input_path).parent
                raise RuntimeError('decode failed')
        self.assertFalse(temporary.exists())

    def test_selected_combined_file_name_collision_falls_back(self):
        combined = self.write('AB.vc3', Path(self.a).read_bytes() + Path(self.b).read_bytes())
        with self.prepare([self.a, self.b, combined]) as jobs:
            self.assertEqual([job.input_path for job in jobs], [self.a, self.b, combined])
            self.assertTrue(all(not job.merged for job in jobs))

    def test_disabled_ground_and_vc3_in_tmtc_mode_preserve_original_order(self):
        for enabled, space, mode in ((False, 1, 2), (True, 2, 2), (True, 1, 1)):
            with patch('GTM_Decoder_Continuous.inspect_vc3', side_effect=AssertionError('must not inspect')):
                with self.prepare([self.b, self.a], enabled, space, mode) as jobs:
                    self.assertEqual([job.input_path for job in jobs], [self.b, self.a])

    def test_merge_failure_and_native_size_limit_fall_back_to_individual_files(self):
        for failure in (patch('GTM_Decoder_Continuous.tempfile.TemporaryDirectory', side_effect=OSError('full disk')),
                        patch('GTM_Decoder_Continuous.MAX_DECODER_INPUT_SIZE', 1)):
            with failure, self.prepare([self.a, self.b]) as jobs:
                self.assertEqual([job.input_path for job in jobs], [self.a, self.b])
                self.assertTrue(all(not job.merged for job in jobs))

    def worker(self, files, checked=True, plot=False):
        window = QtWidgets.QMainWindow()
        ui = Ui_MainWindow()
        ui.setupUi(window)
        ui.Continuous_Files_on_checkBox.setChecked(checked)
        ui.decoder_real_time_display_on_check_box.setChecked(plot)
        ui.decoder_science_export_pipeline_radio_button.setChecked(True)
        ui.decoder_data_import_science_radio_button.setChecked(True)
        ui.decoder_update_time_group.setEnabled(False)
        controller = SimpleNamespace(ui=ui, decoder_cached_input_file_list=list(files),
                                     in_space_flag=1, decode_mode=2, export_mode=2)
        worker = UiDecoderThread(controller)
        worker.test_window = window
        self.addCleanup(window.close)
        return worker

    @staticmethod
    def fake_decode(filename, *args, **kwargs):
        Path(filename + '_science_pipeline.csv').write_text('header\nvalue\n')
        return Path(filename).stat().st_size

    def test_checkbox_merges_only_selected_files_and_preserves_selection(self):
        worker = self.worker([self.b, self.a])
        with patch('GTM_SDC_UI_Controller_Decoder_Thread.c_decoder', side_effect=self.fake_decode) as decode:
            worker.decoder_start()
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(Path(decode.call_args.args[0]).name, 'AB.vc3')
        self.assertEqual(worker.parent.decoder_cached_input_file_list, [self.b, self.a])
        self.assertTrue((self.folder / 'AB.vc3_science_pipeline.csv').exists())
        self.assertFalse(Path(decode.call_args.args[0]).exists())

    def test_unchecked_checkbox_decodes_each_original(self):
        worker = self.worker([self.b, self.a], checked=False)
        with patch('GTM_SDC_UI_Controller_Decoder_Thread.c_decoder', side_effect=self.fake_decode) as decode:
            worker.decoder_start()
        self.assertEqual([call.args[0] for call in decode.call_args_list], [self.b, self.a])

    def test_unmatched_failure_does_not_prevent_next_decode(self):
        invalid = self.write('invalid.vc3', b'invalid')
        worker = self.worker([invalid, self.a])
        errors = []
        worker.decoder_thread_error_signal.connect(errors.append)

        def decode(filename, *args, **kwargs):
            if filename == invalid:
                raise DecoderError('invalid science header')
            return self.fake_decode(filename)

        with patch('GTM_SDC_UI_Controller_Decoder_Thread.c_decoder', side_effect=decode) as call:
            worker.decoder_start()
        self.assertEqual(call.call_count, 2)
        self.assertEqual(len(errors), 1)
        self.assertTrue(Path(self.a + '_science_pipeline.csv').exists())

    def test_plot_mode_uses_published_combined_csv_and_name(self):
        worker = self.worker([self.b, self.a], plot=True)
        names, plots = [], []
        worker.decoder_thread_file_signal.connect(names.append)

        def plot(files):
            self.assertTrue(Path(files[0]).exists())
            plots.extend(files)

        with patch('GTM_SDC_UI_Controller_Decoder_Thread.c_decoder', side_effect=self.fake_decode), \
                patch.object(worker, 'decoder_plot_science', side_effect=plot):
            worker.decoder_start()
        self.assertEqual(plots, [str(self.folder / 'AB.vc3_science_pipeline.csv')])
        self.assertEqual(names, [[str(self.folder), 'AB.vc3']])

    def test_split_groups_reach_plot_with_separate_csv_names(self):
        a, b, c, _ = self.restart_selection()
        worker = self.worker([c, b, a], plot=True)
        plots, errors, names = [], [], []
        worker.decoder_thread_error_signal.connect(errors.append)
        worker.decoder_thread_file_signal.connect(names.append)

        def plot(files):
            self.assertTrue(Path(files[0]).exists())
            self.assertEqual(worker.decoder_update_time_s, 0)
            plots.extend(files)

        with patch('GTM_SDC_UI_Controller_Decoder_Thread.c_decoder', side_effect=self.fake_decode) as decode, \
                patch.object(worker, 'decoder_plot_science', side_effect=plot):
            worker.decoder_start()
        expected = {Path(a).stem + Path(b).stem + '_part1.vc3',
                    Path(b).stem + '_part2' + Path(c).stem + '.vc3'}
        self.assertEqual(errors, [])
        self.assertEqual(decode.call_count, 2)
        self.assertEqual({name[1] for name in names}, expected)
        self.assertEqual({Path(path).name for path in plots},
                         {name + '_science_pipeline.csv' for name in expected})

    def test_single_file_first_part_failure_still_decodes_second_part(self):
        _, b, _, _ = self.restart_selection()
        worker = self.worker([b])
        errors = []
        worker.decoder_thread_error_signal.connect(errors.append)

        def decode(filename, *args, **kwargs):
            if '_part1.vc3' in filename:
                raise DecoderError('preceding packet data is missing')
            return self.fake_decode(filename)

        with patch('GTM_SDC_UI_Controller_Decoder_Thread.c_decoder', side_effect=decode) as native:
            worker.decoder_start()
        self.assertEqual(native.call_count, 2)
        self.assertEqual(len(errors), 1)
        self.assertTrue((self.folder / (Path(b).stem + '_part2.vc3_science_pipeline.csv')).exists())

    def test_merged_plot_is_final_but_singleton_keeps_polling(self):
        other = self.write('other.vc3', b''.join(science_frames(3, 90, 100)))
        worker = self.worker([self.b, self.a, other], plot=True)
        ui = worker.parent.ui
        ui.decoder_update_time_group.setEnabled(True)
        ui.decoder_update_time_combo_box.addItem('1')
        ui.decoder_update_time_combo_box.setCurrentText('1')
        intervals = []
        with patch('GTM_SDC_UI_Controller_Decoder_Thread.c_decoder', side_effect=self.fake_decode) as decode, \
                patch.object(worker, 'decoder_plot_science', side_effect=lambda files: intervals.append(worker.decoder_update_time_s)), \
                patch('GTM_SDC_UI_Controller_Decoder_Thread.time.sleep') as sleep:
            worker.decoder_start()
        self.assertEqual(intervals, [0, 1])
        self.assertEqual(decode.call_count, 3)
        sleep.assert_called_once_with(1)

    @unittest.skipUnless(sys.platform == 'win32', 'requires the bundled Windows native decoder')
    def test_native_pipeline_handles_unknown_event_without_raw_output(self):
        frames = bytearray(b''.join(science_frames(2)))
        frames[16:61] = b'\xca' + bytes(41) + b'\xf2\xf5\xfa'
        frames[61:64] = bytes(3)
        path = self.write('unknown_event.vc3', frames)
        with redirect_stdout(StringIO()):
            self.assertEqual(c_decoder(path, 1, 2, 2, 0), len(frames))
        self.assertFalse(Path(path + '_science_raw.txt').exists())
        self.assertTrue(Path(path + '_weird_science.bin').exists())

    @unittest.skipUnless(sys.platform == 'win32', 'requires the bundled Windows native decoder')
    def test_native_exit_is_contained_and_following_call_succeeds(self):
        invalid = self.write('native_invalid.vc3', bytes(1279))
        with redirect_stdout(StringIO()):
            with self.assertRaises(DecoderError):
                c_decoder(invalid, 1, 2, 2, 0)
            pointer = c_decoder(self.a, 1, 2, 2, 0)
        self.assertEqual(pointer, Path(self.a).stat().st_size)
        self.assertTrue(Path(self.a + '_science_pipeline.csv').exists())

    @unittest.skipUnless(os.environ.get('GTM_TEST_VC3_PAIR'), 'optional real VC3 pair')
    def test_real_pair_matches_manual_merge_csv(self):
        sources = [Path(path) for path in os.environ['GTM_TEST_VC3_PAIR'].split(os.pathsep)]
        self.assertEqual(len(sources), 2)
        selected = []
        for source in sources:
            target = self.folder / source.name
            shutil.copyfile(source, target)
            selected.append(str(target))
        manual = self.folder / 'manual.vc3'
        with manual.open('wb') as destination:
            for source in selected:
                with open(source, 'rb') as input_file:
                    shutil.copyfileobj(input_file, destination)
        worker = self.worker(list(reversed(selected)))
        errors = []
        worker.decoder_thread_error_signal.connect(errors.append)
        with redirect_stdout(StringIO()):
            worker.decoder_start()
            c_decoder(str(manual), 1, 2, 2, 0)
        self.assertEqual(errors, [])
        combined_name = ''.join(source.stem for source in sources) + '.vc3_science_pipeline.csv'
        combined = self.folder / combined_name
        reference = Path(str(manual) + '_science_pipeline.csv')

        combined_digest, reference_digest = csv_digest(combined), csv_digest(reference)
        self.assertEqual(combined_digest, reference_digest)
        self.assertGreater(combined_digest[1], 1)
        print(f'Real VC3 validation: {combined.name}, {combined_digest[1] - 1} data rows, '
              'SHA-256 matches manual concatenation.')

    @unittest.skipUnless(os.environ.get('GTM_TEST_VC3_SPLIT_TRIPLE'), 'optional Day 232 split-stream files')
    def test_real_split_triple_matches_manual_csvs(self):
        sources = [Path(path) for path in os.environ['GTM_TEST_VC3_SPLIT_TRIPLE'].split(os.pathsep)]
        self.assertEqual(len(sources), 3)
        selected = []
        for source in sources:
            target = self.folder / source.name
            shutil.copyfile(source, target)
            selected.append(str(target))
        a, b, c = [Path(path).read_bytes() for path in selected]
        cut = 6944970  # Independently verified Day 232 reset at frame 5431.
        expected = {
            sources[0].stem + sources[1].stem + '_part1.vc3': a + b[:cut],
            sources[1].stem + '_part2' + sources[2].stem + '.vc3': b[cut:] + c,
        }
        with self.prepare(list(reversed(selected))) as jobs:
            self.assertEqual(len(jobs), 2)
            for job in jobs:
                self.assertEqual(Path(job.input_path).read_bytes(), expected[Path(job.output_prefix).name])
        worker = self.worker(list(reversed(selected)))
        errors = []
        worker.decoder_thread_error_signal.connect(errors.append)
        with redirect_stdout(StringIO()):
            worker.decoder_start()
        self.assertEqual(errors, [])
        for index, (name, data) in enumerate(expected.items()):
            manual = self.folder / f'manual_stream{index + 1}.vc3'
            manual.write_bytes(data)
            with redirect_stdout(StringIO()):
                c_decoder(str(manual), 1, 2, 2, 0)
            combined = self.folder / (name + '_science_pipeline.csv')
            reference = Path(str(manual) + '_science_pipeline.csv')
            actual_digest = csv_digest(combined)
            self.assertEqual(actual_digest, csv_digest(reference))
            self.assertGreater(actual_digest[1], 1)
            print(f'Real split VC3 validation: {combined.name}, {actual_digest[1] - 1} data rows, '
                  'SHA-256 matches manual split/concatenation.')
        for path, data in zip(selected, (a, b, c)):
            self.assertEqual(Path(path).read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
