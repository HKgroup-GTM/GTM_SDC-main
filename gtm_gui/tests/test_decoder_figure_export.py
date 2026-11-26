"""Exercise science plotting and actual PNG export with synthetic pipeline data."""

from collections import Counter
from itertools import product
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from PyQt5 import QtGui, QtWidgets
import GTM_SDC_UI_Controller_Decoder as decoder_module
from GTM_SDC_UI_Controller_Decoder import UiDecoder
from GTM_SDC_UI_Controller_Decoder_Thread import UiDecoderThread


SENSORS = {f'{module}{sensor}' for module in 'MS' for sensor in range(1, 5)}


class DecoderFigureExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.controller = UiDecoder()
        self.controller.decoder_plot_open_window_list = []
        self.controller.decoder_cached_input_file_dirname = str(self.output)
        self.controller.decoder_cached_input_file_basename = 'sample.vc3'
        self.controller.ui = SimpleNamespace(
            combine_Hg_Lg_on_check_box=self.checkbox(True),
            decoder_plot_sync_on_check_box=self.checkbox(False),
            decoder_auto_save_figure_group=QtWidgets.QGroupBox(),
            decoder_auto_save_figure_on_check_box=self.checkbox(True),
        )
        self.select_sensors(SENSORS)
        self.worker = UiDecoderThread(self.controller)
        self.worker.decoder_update_time_s = 0
        self.worker.decoder_plot_range_min = -1000
        self.worker.decoder_plot_range_max = 2**14
        self.worker.decoder_plot_bin_size = 5
        self.messages = []
        for signal in (
            self.worker.decoder_thread_plot_update_science_hg_lg_combine_signal,
            self.worker.decoder_thread_plot_update_science_hg_signal,
            self.worker.decoder_thread_plot_update_science_lg_signal,
        ):
            signal.connect(self.messages.append)
        self.addCleanup(self.close_layouts)

    def checkbox(self, checked):
        checkbox = QtWidgets.QCheckBox()
        checkbox.setChecked(checked)
        return checkbox

    def select_sensors(self, sensors):
        for module, prefix in (('master', 'M'), ('slave', 'S')):
            setattr(self.controller.ui, f'decoder_display_selection_{module}_group',
                    self.checkbox(any(f'{prefix}{i}' in sensors for i in range(1, 5))))
            for i in range(1, 5):
                setattr(self.controller.ui, f'decoder_display_selection_{module}_s{i}_check_box',
                        self.checkbox(f'{prefix}{i}' in sensors))

    def close_layouts(self):
        for layout in self.controller.decoder_plot_open_window_list:
            layout.clear()
            layout.close()
        for key in list(vars(decoder_module)):
            if key.startswith('self.decoder_plot_science_pg_layout_'):
                delattr(decoder_module, key)
        self.app.processEvents()

    def prepare_plots(self, basename='sample.vc3'):
        # Include both gains on every channel, plus the columns used by combining.
        rows = []
        for module, citiroc, channel, gain in product(range(2), range(2), range(32), range(2)):
            row = dict.fromkeys(('Hour', 'Minute', 'Second', 'Subsecond',
                                 'X', 'Y', 'Z', 'Q1', 'Q2', 'Q3', 'Q4'), 0)
            row.update({'GTM ID': module, 'CITIROC': citiroc, 'Channel': channel,
                        'Gain': gain, 'ADC': 2000, 'PPS': 1,
                        'Fine Time': len(rows), 'Day of Year': 285})
            rows.append(row)
        pipeline = self.output / f'{basename}_science_pipeline.csv'
        pd.DataFrame(rows).to_csv(pipeline, sep=';', index=False)
        self.worker.current_file_dirname = str(self.output)
        self.worker.current_file_basename = basename
        self.worker.filedir_filename_list = [str(self.output), basename]
        self.worker.decoder_initailize_plot_df_skip_number()
        self.messages.clear()
        if not self.controller.decoder_plot_open_window_list:
            self.controller.decoder_plot_open_window_science()
        self.controller.decoder_clear_layout()
        self.worker.decoder_plot_science([str(pipeline)])

    def render_plots(self):
        # Replay signals synchronously so exceptions in the GUI handler fail tests.
        for message in self.messages:
            if not message[0] and self.controller.ui.combine_Hg_Lg_on_check_box.isChecked():
                layout = vars(decoder_module)[f'self.decoder_plot_science_pg_layout_{message[2]}_combine']
                self.assertEqual(len(layout.ci.items), 16, 'Save only after all channels are plotted')
            self.controller.decoder_plot_science_robotic(message)

    def assert_combined_exports(self, sensors, basename='sample.vc3'):
        self.assertEqual(Counter(message[2] for message in self.messages if not message[0]),
                         Counter(sensors), 'Export every selected sensor exactly once')
        self.assert_pngs({f'{basename}.decoder_plot_science_pg_layout_{sensor}_combine.png'
                          for sensor in sensors}, basename)

    def assert_pngs(self, expected, basename='sample.vc3'):
        paths = list(self.output.glob(f'{basename}.*.png'))
        self.assertEqual({path.name for path in paths}, expected)
        for path in paths:
            self.assertFalse(QtGui.QImage(str(path)).isNull(), f'Invalid PNG: {path.name}')

    def test_combined_all_eight_sensors_are_saved(self):
        self.prepare_plots()
        self.render_plots()
        self.assert_combined_exports(SENSORS)

    def test_combined_only_selected_sensors_are_saved(self):
        selected = {'M2', 'M3', 'S1', 'S4'}
        self.select_sensors(selected)
        self.prepare_plots()
        self.render_plots()
        self.assert_combined_exports(selected)

    def test_unchecked_module_is_not_saved(self):
        self.select_sensors({'M1', 'M2', 'S3', 'S4'})
        self.controller.ui.decoder_display_selection_master_group.setChecked(False)
        self.prepare_plots()
        self.render_plots()
        self.assert_combined_exports({'S3', 'S4'})

    def test_auto_save_off_does_not_write_pngs(self):
        self.select_sensors({'M1', 'M2'})
        self.controller.ui.decoder_auto_save_figure_on_check_box.setChecked(False)
        self.prepare_plots()
        self.render_plots()
        self.assert_pngs(set())

    def test_combined_exports_use_each_signals_file_instead_of_stale_cache(self):
        self.select_sensors({'M1', 'M2'})
        for basename in ('first.vc3', 'second.vc3'):
            with self.subTest(basename=basename):
                self.prepare_plots(basename)
                self.render_plots()
                self.assert_combined_exports({'M1', 'M2'}, basename)
        self.assert_pngs(set())

    def test_separate_gain_exports_are_preserved(self):
        self.controller.ui.combine_Hg_Lg_on_check_box.setChecked(False)
        self.prepare_plots()
        self.render_plots()
        self.assert_pngs({f'sample.vc3.decoder_plot_science_pg_layout_{module}_{citiroc}_{gain}.png'
                          for module, citiroc, gain in product(('master', 'slave'), ('a', 'b'), ('hg', 'lg'))})


if __name__ == '__main__':
    unittest.main()
