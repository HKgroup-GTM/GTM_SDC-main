"""Run the native decoder outside the GUI process so C exit() is recoverable."""

import ctypes
import os
from pathlib import Path
import platform
import subprocess
import sys


RESULT_PREFIX = '__GTM_DECODER_FILE_POINTER__='


class DecoderError(RuntimeError):
    pass


def _native_decoder(file_name, in_space_flag, decode_mode, export_mode, initail_file_pointer):
    extension = {'Darwin': 'dylib', 'Linux': 'so', 'Windows': 'dll'}[platform.system()]
    library = Path(__file__).resolve().with_name('GTM_Decoder_Main.' + extension)
    function = ctypes.CDLL(str(library)).decoder
    function.restype = ctypes.c_int
    function.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    return function(str(file_name).encode('utf-8'), in_space_flag, decode_mode,
                    export_mode, initail_file_pointer)


def c_decoder(file_name, in_space_flag, decode_mode, export_mode, initail_file_pointer):
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker',
               str(Path(file_name).resolve()), str(in_space_flag), str(decode_mode),
               str(export_mode), str(initail_file_pointer)]
    environment = os.environ.copy()
    environment['PYTHONIOENCODING'] = 'utf-8'
    result = subprocess.run(
        command, cwd=str(Path(__file__).resolve().parent), env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    pointer = None
    messages = []
    for line in result.stdout.splitlines():
        if line.startswith(RESULT_PREFIX):
            pointer = int(line[len(RESULT_PREFIX):])
        else:
            messages.append(line)
            print(line)
    if result.returncode != 0 or pointer is None:
        detail = '\n'.join(messages[-4:]) or 'No result returned by native decoder.'
        raise DecoderError(f'{Path(file_name).name}: native decoder failed '
                           f'(exit {result.returncode}).\n{detail}')
    return pointer


if __name__ == '__main__':
    if len(sys.argv) != 7 or sys.argv[1] != '--worker':
        raise SystemExit('This module is launched by the decoder worker.')
    result = _native_decoder(sys.argv[2], *map(int, sys.argv[3:]))
    print(f'{RESULT_PREFIX}{result}', flush=True)
