"""Exercise Flow terminal reports with local child processes; no servers needed."""

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from GTM_SDC_UI_Controller_Flow import UiFlow


class FlowLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.chdir, Path.cwd())
        base = Path(self.temp.name)
        (base / 'gui').mkdir()
        self.logs = base / 'level_0' / 'log'
        self.logs.mkdir(parents=True)
        os.chdir(base / 'gui')
        self.flow = UiFlow()
        self.flow.operation_user = 'operator'
        self.flow.operation_host = 'operation.example'
        self.flow.operation_password = 'secret-operation-password'
        self.flow.archiving_user = 'archiver'
        self.flow.archiving_host = 'archive.example'
        self.flow.archiving_password = 'secret-archive-password'

    def run_child(self, callback, logs=None, output='', error='', exit_code=0):
        source = ['from pathlib import Path', 'import sys']
        for name, content in (logs or {}).items():
            path = self.logs / name
            source.extend([
                f'path = Path({str(path)!r})',
                'path.parent.mkdir(parents=True, exist_ok=True)',
                f'path.write_text({content!r}, encoding="utf-8")',
            ])
        source.extend([
            f'print({output!r})', f'print({error!r}, file=sys.stderr)',
            f'sys.exit({exit_code})',
        ])

        def launch(command, **kwargs):
            return subprocess.Popen([sys.executable, '-B', '-c', '\n'.join(source)], **kwargs)

        stream = StringIO()
        with redirect_stdout(stream), patch('GTM_SDC_UI_Controller_Flow.Popen', side_effect=launch) as popen:
            worker = callback()
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive(), 'Flow worker did not finish')
        return stream.getvalue(), popen.call_args.args[0]

    def test_clone_reports_actual_files_from_all_operation_logs(self):
        logs = {
            f'{category}_download_from_operation.log': f'sftp> get {category}_data.bin\nFetching {category}_data.bin\n'
            for category in ('import_mcc', 'export_mcc', 'export_dcc')
        }
        output, command = self.run_child(self.flow.flow_operation_processing_clone, logs)
        self.assertIn('Operation server -> Processing (local)', output)
        self.assertIn('operator@operation.example', output)
        for name, content in logs.items():
            self.assertIn(str(self.logs / name), output)
            self.assertIn(content.strip(), output)
        self.assertEqual(command, [
            './download_from_operation.sh', 'operator', 'operation.example',
            self.flow.operation_password,
        ])
        self.assertNotIn(self.flow.operation_password, output)

    def test_socc_push_prints_downloaded_transfer_log_contents(self):
        output, _ = self.run_child(self.flow.flow_socc_operation_push, {
            'from_operation/import_mcc_upload_to_socc.log':
                'sftp> put FS8B_timeline.xml\nUploading FS8B_timeline.xml to /import_mcc/FS8B_timeline.xml\n',
        })
        self.assertIn('Operation server -> SOCC', output)
        self.assertIn('Uploading FS8B_timeline.xml', output)

    def test_update_prints_refreshed_listing_even_when_contents_are_unchanged(self):
        name = 'processing_import_mcc.log'
        listing = '-rw-r--r-- 1 operator users 12 Oct 6 10:00 timeline.xml\n'
        path = self.logs / name
        path.write_text(listing, encoding='utf-8')
        os.utime(path, (1, 1))
        output, _ = self.run_child(self.flow.flow_operation_processing_update_processing, {name: listing})
        self.assertIn('Update Processing (local) file lists', output)
        self.assertIn(listing.strip(), output)

    def test_missing_and_stale_logs_do_not_replay_old_transfers(self):
        (self.logs / 'import_mcc_download_from_operation.log').write_text('get old.bin\n')
        output, _ = self.run_child(self.flow.flow_operation_processing_clone)
        self.assertNotIn('old.bin', output)
        self.assertIn('Log was not updated', output)
        self.assertIn('Log unavailable', output)

    def test_empty_log_is_explicit(self):
        output, _ = self.run_child(self.flow.flow_operation_processing_push, {
            'import_mcc_upload_to_operation.log': '',
        })
        self.assertIn('No entries recorded (empty log)', output)

    def test_errors_and_partial_logs_are_visible_without_passwords(self):
        output, _ = self.run_child(self.flow.flow_operation_processing_push, {
            'import_mcc_upload_to_operation.log': 'sftp> put denied.xml\nPermission denied\n',
        }, output=self.flow.operation_password, error='Connection lost', exit_code=1)
        self.assertIn('Script exit code: 1', output)
        self.assertIn('Permission denied', output)
        self.assertIn('Connection lost', output)
        self.assertNotIn(self.flow.operation_password, output)
        self.assertNotIn('success', output.lower())

    def test_archiving_push_includes_level_one_and_two(self):
        output, command = self.run_child(self.flow.flow_processing_archiving_push, {
            'to_archiving/level_1_upload_to_archiving.log': 'put calibrated.csv\n',
            'to_archiving/level_2_upload_to_archiving.log': 'put lightcurve.png\n',
        }, output=self.flow.archiving_password)
        self.assertIn('calibrated.csv', output)
        self.assertIn('lightcurve.png', output)
        self.assertIn('archiver@archive.example', output)
        self.assertEqual(command[-1], self.flow.archiving_password)
        self.assertNotIn(self.flow.archiving_password, output)

    def test_launch_failure_is_reported_and_allows_retry(self):
        stream = StringIO()
        with redirect_stdout(stream), patch('GTM_SDC_UI_Controller_Flow.Popen', side_effect=OSError('Missing interpreter')):
            worker = self.flow.flow_operation_processing_push()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertIn('[Flow] FAILED:', stream.getvalue())
        self.assertIn('Missing interpreter', stream.getvalue())
        output, _ = self.run_child(self.flow.flow_operation_processing_push)
        self.assertIn('[Flow] RESULT:', output)

    def test_running_transfer_does_not_block_callback_or_start_duplicate(self):
        entered = Event()
        release = Event()

        def communicate():
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError('Test did not release child process')
            return '', None

        process = Mock(returncode=0)
        process.communicate.side_effect = communicate
        stream = StringIO()
        with redirect_stdout(stream), patch('GTM_SDC_UI_Controller_Flow.Popen', return_value=process) as popen:
            worker = self.flow.flow_operation_processing_clone()
            try:
                self.assertTrue(entered.wait(timeout=2))
                self.assertTrue(worker.is_alive())
                self.assertIsNone(self.flow.flow_operation_processing_clone())
                popen.assert_called_once()
            finally:
                release.set()
                worker.join(timeout=5)
        self.assertIn('Already running', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
