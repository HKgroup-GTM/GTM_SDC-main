"""Regression checks for ICD table 5-1 OBC selection and per-orbit DAC settings.

Run with the GUI environment: python -B -m unittest discover -s tests -v
"""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
import xml.etree.ElementTree as ET

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from PyQt5 import QtCore, QtWidgets
from GTM_SDC_CMD_Config import DEFAULT_DAC_VALUES
from GTM_SDC_UI import Ui_MainWindow
from GTM_SDC_UI_Controller_Mtl_Cmd import UiMtlCmd
from GTM_SDC_Specific_MTL_Controller import SpecificMTLWindow
from GTM_SDC_specific_CMD_DAC_HV_setting_Controller import CmdDacHvSettingWindow


def commands(procedure):
    return [
        [int(value, 16) for value in re.findall(r'0x([0-9A-F]{2})', line['content'])]
        for section in procedure['sections'] for line in section['lines']
        if 'GTM_CFG' in line['content']
    ]


class MtlCmdConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        # Exercise the real writers without touching operational output files.
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.old_cwd)
        base = Path(self.temp.name)
        (base / 'gui').mkdir()
        self.output = base / 'level_0' / 'import_mcc'
        self.output.mkdir(parents=True)
        os.chdir(base / 'gui')
        self.gen = UiMtlCmd()
        self.gen.mtl_start_utc = datetime(2026, 9, 30)
        self.gen.mtl_on_off_minutes_group = [[0, 30]]

    def read_procedure(self, index=1):
        return json.loads((self.output / f'FS8BSPL_PROC2026273F{index}.json').read_text())

    def assert_valid_commands(self, packets, obc):
        for packet in packets:
            self.assertEqual(len(packet), 128)
            self.assertEqual(packet[:2], [0x55, 0xAA])
            self.assertEqual(packet[-2:], [0xFB, 0xF2])
            self.assertEqual(packet[119], {'OBCA': 0x00, 'OBCB': 0x03}[obc])
            self.assertEqual(packet[125], sum(packet[2:125]) % 256)

    def test_obc_sets_exact_legacy_cmd_sel_byte_and_updates_checksum(self):
        for formatter in (self.gen.format_gtm_cfg_command, self.gen.format_gtm_cfg_command_json):
            for initial in range(256):
                for obc, value in (('OBCA', 0x00), ('OBCB', 0x03)):
                    packet = list(range(128))
                    packet[119] = initial
                    before = packet[:]
                    formatter(packet, obc=obc)
                    self.assertEqual(packet[119], value)
                    self.assertEqual(packet[125], sum(packet[2:125]) % 256)
                    self.assertEqual([n for n in range(128) if packet[n] != before[n] and n not in (119, 125)], [])
            with self.assertRaises(ValueError):
                formatter([0] * 127)

    def test_main_dropdown_applies_to_all_on_off_commands(self):
        window = QtWidgets.QMainWindow()
        self.addCleanup(window.close)
        ui = Ui_MainWindow()
        ui.setupUi(window)
        self.gen.ui = ui
        self.assertEqual(ui.cmd_obc_combo.currentText(), 'OBCA')
        self.assertEqual([ui.cmd_obc_combo.itemText(i) for i in range(2)], ['OBCA', 'OBCB'])
        ui.cmd_obc_combo.currentTextChanged.connect(self.gen.cmd_obc_changed)
        self.gen.cmd_write_on_xml()
        a = commands(self.read_procedure())
        self.assertEqual(len(a), 13)
        self.assert_valid_commands(a, 'OBCA')
        ui.cmd_generate_button.setEnabled(False)
        ui.cmd_obc_combo.setCurrentText('OBCB')
        self.assertTrue(ui.cmd_generate_button.isEnabled())
        self.gen.cmd_write_on_xml()
        b = commands(self.read_procedure())
        self.assert_valid_commands(b, 'OBCB')
        for original, changed in zip(a, b):
            self.assertEqual([i for i in range(128) if original[i] != changed[i]], [119, 125])
            self.assertEqual(changed[125], (original[125] + 0x03) % 256)
        for packet, group in zip(b[4:8], ('M_A', 'M_B', 'S_A', 'S_B')):
            self.assertEqual(packet[44:76], list(DEFAULT_DAC_VALUES[group]))
        self.assertEqual([p[123] for p in b[4:8]], [0xA4, 0xA5, 0xAD, 0xA9])

    def test_dac_editor_and_designer_defaults_match_all_channels(self):
        config = {}
        window = CmdDacHvSettingWindow(config, 1)
        self.addCleanup(window.close)
        source = ET.parse(ROOT / 'GTM_SDC_specific_CMD_DAC_HV_setting.ui')
        for group, values in DEFAULT_DAC_VALUES.items():
            self.assertEqual(len(values), 32)
            for ch, value in enumerate(values):
                name = f'{group}_ch{ch}'
                self.assertEqual(getattr(window.ui, name).text(), f'{value:02X}')
                self.assertEqual(source.find(f'.//widget[@name="{name}"]/property[@name="text"]/string').text, f'{value:02X}')
        window.ui.M_A_ch0.setText('AB')
        window._save_and_close()
        reopened = CmdDacHvSettingWindow(config, 1)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.ui.M_A_ch0.text(), 'AB')

    def specific_window(self):
        mc = SimpleNamespace(start_time_flag=2, assign_utc='2026-09-30 00:00:00')
        window = SpecificMTLWindow(mc)
        self.addCleanup(window.close)
        return window

    @staticmethod
    def row(window, index):
        return window.ui.verticalLayout.itemAt(index).widget()

    def set_timing(self, row, start_minute, end_minute):
        layout = row.layout()
        layout.itemAtPosition(0, 1).widget().setChecked(True)
        base = QtCore.QDateTime(2026, 9, 30, 0, 0)
        layout.itemAtPosition(0, 2).widget().setDateTime(base.addSecs(start_minute * 60))
        layout.itemAtPosition(0, 3).widget().setDateTime(base.addSecs(end_minute * 60))

    def test_specific_rows_keep_independent_obc_and_custom_dac(self):
        window = self.specific_window()
        first = self.row(window, 1)
        first.layout().itemAtPosition(0, 9).widget().click()
        second = self.row(window, 2)
        self.set_timing(first, 0, 30)
        self.set_timing(second, 90, 120)
        second.layout().itemAtPosition(0, 8).widget().setCurrentText('OBCB')
        window._cmd_config_data[second].update(M_A_ch0='AB', S_B_ch31='CD', M_B_HV_DAC_setting='A6')
        window.generate()
        a, b = commands(self.read_procedure(1)), commands(self.read_procedure(2))
        self.assert_valid_commands(a, 'OBCA')
        self.assert_valid_commands(b, 'OBCB')
        self.assertEqual(a[4][44], DEFAULT_DAC_VALUES['M_A'][0])
        self.assertEqual(b[4][44], 0xAB)
        self.assertEqual(b[7][75], 0xCD)
        self.assertEqual(b[5][123], 0xA6)
        timeline = json.loads((self.output / 'FS8B_SDC_20260930_timeline.json').read_text())
        self.assertEqual([event['dur'] for event in timeline['events']], [1800, 1800])

    def test_filtered_intervals_inherit_row_obc(self):
        window = self.specific_window()
        window.main_controller.mtl_input_file = 'test.tle'
        window.main_controller.period_mins = 5
        row = self.row(window, 1)
        row.layout().itemAtPosition(0, 8).widget().setCurrentText('OBCB')
        backend = 'GTM_SDC_UI_Controller_Mtl_Cmd_Backend.'
        with patch(backend + 'load_tle', return_value=[]), \
             patch(backend + 'calculate_orbit_eclipse', return_value=(None, np.zeros((5, 2)), [False, False, True, False, False], np.arange(5), None)), \
             patch(backend + 'circle_saa', return_value=([], None)), \
             patch(backend + 'in_saa', return_value=([False] * 5, [], [])):
            window.generate()
        self.assertEqual(window.gen.mtl_on_off_minutes_group, [[0.0, 1.0], [3.0, 4.0]])
        for i in (1, 2):
            self.assert_valid_commands(commands(self.read_procedure(i)), 'OBCB')

    def test_add_after_tenth_row_and_remove_preserve_choices(self):
        window = self.specific_window()
        for _ in range(10):
            self.row(window, window.row_count).layout().itemAtPosition(0, 9).widget().click()
        tenth = self.row(window, 10)
        last = self.row(window, 11)
        tenth.layout().itemAtPosition(0, 8).widget().setCurrentText('OBCB')
        tenth.layout().itemAtPosition(0, 9).widget().click()
        inserted = self.row(window, 11)
        self.assertIs(self.row(window, 10), tenth)
        self.assertIs(self.row(window, 12), last)
        self.assertEqual(inserted.layout().itemAtPosition(0, 8).widget().currentText(), 'OBCA')
        inserted.layout().itemAtPosition(0, 10).widget().click()
        self.assertEqual(window.row_count, 11)
        self.assertIs(self.row(window, 11), last)
        self.assertEqual(window._cmd_config_data[tenth]['obc'], 'OBCB')
        self.assertNotIn(inserted, window._cmd_config_data)


if __name__ == '__main__':
    unittest.main()
