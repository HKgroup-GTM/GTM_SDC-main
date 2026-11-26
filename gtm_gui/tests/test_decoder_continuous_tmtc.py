"""Selected-only TMTC matching, packet fragments, resets and native CSV parity."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt5 import QtWidgets
from GTM_Decoder_Continuous import group_selected_files, inspect_tmtc_segments, prepare_decode_inputs
from GTM_SDC_UI import Ui_MainWindow
from GTM_SDC_UI_Controller_Decoder_Thread import UiDecoderThread
from GTM_SDC_UI_Controller_Decoder_Thread_C import c_decoder, DecoderError


SUFFIXES = ('_tmtc_all.csv', '_tmtc_master.csv', '_tmtc_slave.csv')


def tmtc_packets(count, sequence=1, counter=3, start=datetime(2026, 10, 12, 1), step=64):
    result = []
    for index in range(count):
        packet = bytearray(144)
        packet[:2] = b'\x08\x91'
        packet[2:4] = (0xc000 | ((sequence + index) % 16384)).to_bytes(2, 'big')
        packet[4:6] = b'\x00\x89'
        ticks = index * step
        moment = start + timedelta(seconds=ticks // 256)
        packet[6:8] = moment.year.to_bytes(2, 'big')
        packet[8:10] = moment.timetuple().tm_yday.to_bytes(2, 'big')
        packet[10:14] = bytes((moment.hour, moment.minute, moment.second, ticks % 256))
        packet[16:18] = b'\x55\xaa'
        packet[18] = 5 if index % 2 == 0 else 2
        packet[19:21] = ((counter + index) % 65536).to_bytes(2, 'big')
        packet[21:23] = b'\x00\x78'
        packet[-2:] = b'\xfb\xf2'
        result.append(bytes(packet))
    return result


class ContinuousTMTCTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.data = b''.join(tmtc_packets(18))
        self.a = self.write('A.dmp', self.data[:6 * 144])
        self.b = self.write('B.dmp', self.data[6 * 144:12 * 144])
        self.c = self.write('C.dmp', self.data[12 * 144:])
        self.messages = []

    def write(self, name, data):
        path = self.folder / name
        path.write_bytes(data)
        return str(path)

    def prepare(self, paths, enabled=True, space=1):
        return prepare_decode_inputs(paths, enabled, space, 1, self.messages.append)

    def groups(self, paths):
        return [[s.path for s in group]
                for group in group_selected_files(paths, self.messages.append, decode_mode=1)[0]]

    def worker(self, files, checked=True, plot=False):
        window = QtWidgets.QMainWindow()
        ui = Ui_MainWindow()
        ui.setupUi(window)
        ui.Continuous_Files_on_checkBox.setChecked(checked)
        ui.decoder_real_time_display_on_check_box.setChecked(plot)
        ui.decoder_science_export_pipeline_radio_button.setChecked(True)
        ui.decoder_data_import_tmtc_radio_button.setChecked(True)
        ui.decoder_display_selection_master_group.setChecked(True)
        ui.decoder_display_selection_slave_group.setChecked(True)
        ui.decoder_update_time_group.setEnabled(False)
        controller = SimpleNamespace(ui=ui, decoder_cached_input_file_list=list(files),
                                     in_space_flag=1, decode_mode=1, export_mode=2)
        worker = UiDecoderThread(controller)
        worker.test_window = window
        self.addCleanup(window.close)
        return worker

    @staticmethod
    def fake_decode(filename, *args, **kwargs):
        for suffix in SUFFIXES:
            Path(filename + suffix).write_text('header\nvalue\n')
        return Path(filename).stat().st_size

    def test_reversed_selection_exact_bytes_name_all_csvs_and_cleanup(self):
        with self.prepare([self.c, self.b, self.a]) as jobs:
            self.assertEqual(len(jobs), 1)
            job = jobs[0]
            self.assertEqual(job.sources, (self.a, self.b, self.c))
            self.assertEqual(Path(job.input_path).read_bytes(), self.data)
            self.assertEqual(job.output_prefix, str(self.folder / 'ABC.dmp'))
            self.fake_decode(job.input_path)
            job.publish_outputs()
            for suffix in SUFFIXES:
                self.assertEqual(Path(job.output_prefix + suffix).read_text(), 'header\nvalue\n')
        self.assertFalse(Path(job.input_path).exists())
        self.assertFalse(Path(job.output_prefix).exists())
        self.assertEqual(Path(self.a).read_bytes() + Path(self.b).read_bytes() + Path(self.c).read_bytes(), self.data)

    def test_only_selected_files_and_ambiguous_continuations(self):
        self.assertEqual(self.groups([self.c, self.a]), [[self.c], [self.a]])
        duplicate = self.write('B_copy.dmp', Path(self.b).read_bytes())
        self.assertEqual(self.groups([self.a, self.b, duplicate]), [[self.a], [self.b], [duplicate]])

    def test_both_counter_wraps_and_year_rollover(self):
        data = b''.join(tmtc_packets(12, 16380, 65532, datetime(2026, 12, 31, 23, 59, 59)))
        a = self.write('wrap1.dmp', data[:4 * 144])
        b = self.write('wrap2.dmp', data[4 * 144:])
        with self.prepare([b, a]) as jobs:
            self.assertEqual(len(jobs), 1)
            self.assertEqual(Path(jobs[0].input_path).read_bytes(), data)

    def test_cross_file_partial_packets_are_reconstructed_without_trimming(self):
        for split in (1, 2, 5, 17, 50, 143):
            with self.subTest(split=split):
                cut = 6 * 144 + split
                Path(self.a).write_bytes(self.data[:cut])
                Path(self.b).write_bytes(self.data[cut:])
                with self.prepare([self.b, self.a]) as jobs:
                    self.assertEqual(len(jobs), 1)
                    self.assertEqual(Path(jobs[0].input_path).read_bytes(), self.data)

    def test_damaged_crossing_packet_is_not_joined(self):
        data = bytearray(self.data)
        data[7 * 144 - 1] = 0  # Tail of the split packet, outside complete packets on either side.
        cut = 6 * 144 + 50
        Path(self.a).write_bytes(data[:cut])
        Path(self.b).write_bytes(data[cut:])
        self.assertEqual(self.groups([self.a, self.b]), [[self.a], [self.b]])

    def test_malformed_or_discontinuous_inputs_fall_back(self):
        original = Path(self.b).read_bytes()
        for offset, value in ((0, 0), (2, 0), (5, 0), (16, 0), (18, 7), (22, 0), (143, 0),
                              (10, 24), (8, 255), (144 + 3, 50), (144 + 20, 50), (144 + 13, 0)):
            with self.subTest(offset=offset):
                damaged = bytearray(original)
                damaged[offset] = value
                Path(self.b).write_bytes(damaged)
                with self.prepare([self.a, self.b]) as jobs:
                    self.assertEqual([job.input_path for job in jobs], [self.a, self.b])
        Path(self.b).write_bytes(original)

    def test_counter_alias_after_long_time_gap_is_not_joined(self):
        later = b''.join(tmtc_packets(6, 7, 9, datetime(2026, 10, 13, 1)))
        Path(self.b).write_bytes(later)
        self.assertEqual(self.groups([self.a, self.b]), [[self.a], [self.b]])

    def test_filename_spacecraft_and_time_conflicts_are_not_joined(self):
        a = self.write('FS8B_SPL_2026285011945_145.dmp', Path(self.a).read_bytes())
        for name in ('FS9B_SPL_2026285020238_145.dmp', 'FS8B_SPL_2026285010000_145.dmp',
                     'FS8B_SPL_2026285020238_146.dmp'):
            b = self.write(name, Path(self.b).read_bytes())
            self.assertEqual(self.groups([a, b]), [[a], [b]])

    def restart_selection(self):
        first = b''.join(tmtc_packets(16))
        second = b''.join(tmtc_packets(12, start=datetime(2026, 10, 12, 2)))
        a = self.write('before.dmp', first[:8 * 144])
        b = self.write('restart.dmp', first[8 * 144:] + second[:6 * 144])
        c = self.write('after.dmp', second[6 * 144:])
        return a, b, c, first, second

    def test_internal_restart_splits_middle_file_into_two_groups(self):
        a, b, c, first, second = self.restart_selection()
        self.assertEqual([(s.start, s.stop, s.part) for s, _ in inspect_tmtc_segments(b)],
                         [(0, 8 * 144, 1), (8 * 144, 14 * 144, 2)])
        with self.prepare([c, b, a]) as jobs:
            self.assertEqual({Path(j.output_prefix).name: Path(j.input_path).read_bytes() for j in jobs},
                             {'beforerestart_part1.dmp': first, 'restart_part2after.dmp': second})
        self.assertTrue(any('TMTC stream restart' in message for message in self.messages))

    def test_single_selected_file_can_split(self):
        _, b, _, _, _ = self.restart_selection()
        with self.prepare([b]) as jobs:
            self.assertEqual([Path(j.output_prefix).name for j in jobs],
                             ['restart_part1.dmp', 'restart_part2.dmp'])
            self.assertEqual(b''.join(Path(j.input_path).read_bytes() for j in jobs), Path(b).read_bytes())

    def test_unconfirmed_restarts_fall_back(self):
        _, b, _, _, _ = self.restart_selection()
        original = Path(b).read_bytes()
        for case in ('icd_clock_set', 'counter_not_reset', 'too_short', 'following_gap'):
            data = bytearray(original)
            cut = 8 * 144
            if case == 'icd_clock_set':
                data[cut + 23] = 7
            elif case == 'counter_not_reset':
                data[cut + 20] = 50
            elif case == 'too_short':
                data = data[:cut + 2 * 144]
            else:
                data[cut + 144 + 3] = 50
            Path(b).write_bytes(data)
            with self.subTest(case=case), self.prepare([b]) as jobs:
                self.assertEqual([j.input_path for j in jobs], [b])

    def test_tail_retained_and_reported(self):
        Path(self.b).write_bytes(Path(self.b).read_bytes() + self.data[12 * 144:12 * 144 + 30])
        with self.prepare([self.a, self.b]) as jobs:
            self.assertEqual(Path(jobs[0].input_path).read_bytes(), self.data[:12 * 144 + 30])
        self.assertTrue(any('30 trailing TMTC bytes' in message for message in self.messages))

    def test_disabled_and_ground_do_not_inspect_or_split(self):
        _, b, _, _, _ = self.restart_selection()
        for enabled, space in ((False, 1), (True, 2)):
            with patch('GTM_Decoder_Continuous.inspect_tmtc_segments', side_effect=AssertionError('unexpected scan')):
                with self.prepare([b, self.a], enabled, space) as jobs:
                    self.assertEqual([j.input_path for j in jobs], [b, self.a])

    def test_staging_failure_size_limit_and_name_collision_fall_back(self):
        for failure in (patch('GTM_Decoder_Continuous.tempfile.TemporaryDirectory', side_effect=OSError('full disk')),
                        patch('GTM_Decoder_Continuous.MAX_DECODER_INPUT_SIZE', 1)):
            with failure, self.prepare([self.a, self.b]) as jobs:
                self.assertEqual([j.input_path for j in jobs], [self.a, self.b])
        combined = self.write('AB.dmp', self.data[:12 * 144])
        with self.prepare([self.a, self.b, combined]) as jobs:
            self.assertEqual([j.input_path for j in jobs], [self.a, self.b, combined])

    def test_worker_checkbox_and_plot_use_combined_tmtc_csvs(self):
        worker = self.worker([self.b, self.a], plot=True)
        plots, names = [], []
        worker.decoder_thread_file_signal.connect(names.append)
        worker.parent.ui.decoder_update_time_group.setEnabled(True)
        worker.parent.ui.decoder_update_time_combo_box.setCurrentText('1')

        def plot(files):
            self.assertTrue(all(Path(path).exists() for path in files))
            self.assertEqual(worker.decoder_update_time_s, 0)
            plots.extend(files)

        with patch('GTM_SDC_UI_Controller_Decoder_Thread.c_decoder', side_effect=self.fake_decode) as decode, \
                patch.object(worker, 'decoder_plot_tmtc', side_effect=plot):
            worker.decoder_start()
        decode.assert_called_once()
        self.assertEqual(plots, [str(self.folder / ('AB.dmp' + suffix)) for suffix in SUFFIXES[1:]])
        self.assertEqual(names, [[str(self.folder), 'AB.dmp']])
        self.assertEqual(worker.parent.decoder_cached_input_file_list, [self.b, self.a])

    def test_worker_unchecked_preserves_individual_decoding(self):
        worker = self.worker([self.b, self.a], checked=False)
        with patch('GTM_SDC_UI_Controller_Decoder_Thread.c_decoder', side_effect=self.fake_decode) as decode:
            worker.decoder_start()
        self.assertEqual([call.args[0] for call in decode.call_args_list], [self.b, self.a])

    def test_worker_failure_does_not_prevent_next_group(self):
        invalid = self.write('invalid.dmp', bytes(300))
        worker = self.worker([invalid, self.b, self.a])
        errors = []
        worker.decoder_thread_error_signal.connect(errors.append)

        def decode(filename, *args, **kwargs):
            if filename == invalid:
                raise DecoderError('invalid TMTC header')
            return self.fake_decode(filename)

        with patch('GTM_SDC_UI_Controller_Decoder_Thread.c_decoder', side_effect=decode) as call:
            worker.decoder_start()
        self.assertEqual(call.call_count, 2)
        self.assertEqual(len(errors), 1)
        self.assertTrue((self.folder / 'AB.dmp_tmtc_all.csv').exists())

    @unittest.skipUnless(sys.platform == 'win32', 'requires the bundled Windows native decoder')
    def test_native_partial_packet_merge_matches_manual_csvs(self):
        cut = 6 * 144 + 50
        Path(self.a).write_bytes(self.data[:cut])
        Path(self.b).write_bytes(self.data[cut:])
        worker = self.worker([self.b, self.a])
        errors = []
        worker.decoder_thread_error_signal.connect(errors.append)
        manual = self.write('manual.dmp', self.data)
        with redirect_stdout(StringIO()):
            worker.decoder_start()
            c_decoder(manual, 1, 1, 2, 0)
        self.assertEqual(errors, [])
        for suffix, rows in zip(SUFFIXES, (18, 9, 9)):
            actual = (self.folder / ('AB.dmp' + suffix)).read_bytes()
            self.assertEqual(actual, Path(manual + suffix).read_bytes())
            self.assertEqual(actual.count(b'\n') - 1, rows)

    @unittest.skipUnless(os.environ.get('GTM_TEST_TMTC_FILES'), 'optional seven Day 285 TMTC files')
    def test_real_selection_matches_manual_groups_for_all_csvs(self):
        sources = [Path(path) for path in os.environ['GTM_TEST_TMTC_FILES'].split(os.pathsep)]
        self.assertEqual(len(sources), 7)
        selected = []
        for source in sources:
            target = self.folder / source.name
            shutil.copyfile(source, target)
            selected.append(str(target))
        expected_groups = [[selected[0]], selected[1:3], selected[3:5], selected[5:7]]
        groups = self.groups(list(reversed(selected)))
        self.assertEqual({tuple(group) for group in groups}, {tuple(group) for group in expected_groups})
        worker = self.worker(list(reversed(selected)))
        errors = []
        worker.decoder_thread_error_signal.connect(errors.append)
        with redirect_stdout(StringIO()):
            worker.decoder_start()
        self.assertEqual(errors, [])
        for index, group in enumerate(expected_groups):
            data = b''.join(Path(path).read_bytes() for path in group)
            manual = self.write(f'manual{index}.dmp', data)
            with redirect_stdout(StringIO()):
                c_decoder(manual, 1, 1, 2, 0)
            name = ''.join(Path(path).stem for path in group) + '.dmp'
            row_counts = []
            for suffix in SUFFIXES:
                actual = (self.folder / (name + suffix)).read_bytes()
                expected = Path(manual + suffix).read_bytes()
                self.assertEqual(hashlib.sha256(actual).digest(), hashlib.sha256(expected).digest())
                row_counts.append(actual.count(b'\n') - 1)
            self.assertEqual(row_counts[0], len(data) // 144)
            self.assertEqual(row_counts[0], row_counts[1] + row_counts[2])
            print(f'Real TMTC validation: {name}: all/master/slave rows {row_counts}; '
                  'all three CSV SHA-256 values match manual concatenation.')
        for source, copied in zip(sources, selected):
            self.assertEqual(source.read_bytes(), Path(copied).read_bytes())


if __name__ == '__main__':
    unittest.main()
