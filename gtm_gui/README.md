# GTM/SDC - Decoder
In this document, we will learn how to install and use the decoder developed by NTHU GTM team. This decoder is a cross-platform software, supporting MacOS, Linux and Windows. We can find more details in [Installation](#Installation) section. The purpose of this software is to decode those binary data recorded by GTM on Formosat-8B to human-readable data based on internal GTM definition. Please go [Utility](#Utility) section to understand more.

## Installation

To speed up the decoding process, we use C language to handle heavy memory calculations. In addition, we imported Qt5 libary in Python language to build a friendly user interface (UI) to prevent the user from typing a lot of trivial commands in Terminal to trigger the decoder, such as TeleMetry and TeleCommand (TM/TC) or Science Data (SD), raw or pipeline SD, ..., etc. Therefore, please pre-install [GCC](#GCC) and [Python](#Python) in the device, then we can [Install and Execute Decoder](#Install-and-Execute-Decoder) smoothly.

### GCC

The GNU Compiler Collection (GCC), as it literally means, is a collection of compilers produced by the GNU project, which allowing developers around the world can use this free and powerful tool to compile various programming languages, including C. Furthermore, since our C programs need to be called from Python, instead of linking all compiled object files (.o) into an executable file (.out) with GCC, we actually use GCC to link all compiled object file (.o) together to create a dynamic library (different suffix in each Operation System (OS), please refer to [Install and Execute Decoder](#Install-and-Execute-Decoder) section) to call.

For installation, the process is simpler for <a href="#GCC-MacOS">MacOS</a> and <a href="#GCC-Linux">Linux</a> than <a href="#GCC-Windows">Windows</a> because GNU is Unix-like OS. If the device has not yet been installed with GCC, please move to the corresponding section to learn it.

<a name="GCC-MacOS"></a> 

- #### MacOS

  There are actually two approaches, Xcode and Homebrew, that can help us install GCC in MacOS. However, despite the fact that the former is not essentially GCC (it is Apple's Clang compiler, intentionally designed to be compatible with GCC), the latter takes longer to install and also requires changing the alias from `gcc-#` (# is the version number of GCC) to `gcc`, which is rather inconvenient. Therefore, we recommend using Xcode to install GCC.

  - Open App Store

  - Download Xcode

  - Install GCC (two methods)

    - Method 1: Run the following command in Terminal

      ```
      xcode-select --install
      ```

    - Method 2: Xcode app > Preferences > Downloads > Install Command Line Tools

  - Run `gcc --version` in Terminal to verify a successful installation

<a name="GCC-Linux"></a>

- #### Linux

  - Run the following commands in Terminal

    ```
    sudo apt update
    sudo apt install build-essential
    ```

  - Run `gcc --version` in Terminal to verify a successful installation

<a name="GCC-Windows"></a>

- #### Windows

  The Minimalist GNU for Windows (MinGW) and Cygwin (formerly known as gnuwin32, renamed Cygwin32 to avoid confusion with another GnuWin32 project to emphasize Cygnus' role in developing it , whose number was dropped when Microsoft registered the Win32 trademark) are two well-known projects that provide GCC functionality on Windows.

  Currently, most modern OS prefer 64-bit (because of its faster data speed and ability to address more memory) and use 64-bit edition of Python by default. As a result, MinGW (which only supports 32-bit Windows) is no longer practical. Fortunately, we still can use advanced MinGW-w64 (which supports 32 and 64-bit Windows) to substitute MinGW. 

  Considering that Cygwin effectively provides a Unix-like environment on 32 and 64-bit Windows, it is unfriendly for native Windows users to learn Unix commands. For this reason, we recommend using MinGW-w64 to compile code in Command Prompt on Windows.

  There are several ways can install MinGW-w64. We recommend installing MinGW-w64 by Cygwin (which contains **mingw64-x86_64-gcc-core** and **make** as packages) to take advantage of the graphical user interface (GUI) that Windows is known for to help us simplify the tedious installation process. This approach also eliminates the need to manually set environment variables.

  - Go [Cygwin](https://cygwin.com/install.html) to download **setup-x86_64.exe**
  - Open **setup-x86_64.exe**
  - Click "Next" until see "Select Package"
  - Select "Full" in "View"
  - Type "gcc" in "Search" > Find **mingw64-x86_64-gcc-core** in "Package" > Change "Skip" to lastet version in "New"
  - Type "make" in "Search" > Find **make** in "Package" > Change "Skip" to lastet version in "New"
  - Click "Next" until see "Finish" > Click "Finish"

### Python 

Using miniconda to manage a new clear Python environment

### Install and Execute Decoder

```
cd <your_gui_folder>
make -f Makefile_<your_OS>

conda create -n env_gtm python=3.9 –y
conda activate env_gtm
conda install anaconda::numpy -y
conda install anaconda::pandas -y
conda install conda-forge::matplotlib -y
conda install anaconda::pyqt -y
conda install conda-forge::pyqtgraph -y
conda install anaconda::scipy -y
conda install anaconda::scikit-learn -y
conda install anaconda::astropy -y
conda install conda-forge::skyfield -y
(Linux should use "pip install skyfield”!)
conda install esri::pyquaternion -y
conda install conda-forge::shapely -y

python GTM_SDC_Start.py
```



## Utility

### Continuous science and TMTC files

In **Decoder**, select the input files and check **Continuous Files > On** before
pressing **Start**. Matching applies to **In Space / Science** `.vc3` and
**In Space / TMTC** `.dmp` inputs. Only selected files are inspected; the containing
folder is never searched. Ground inputs retain individual decoding.

For **Science** VC3 inputs, the decoder checks only
the selected files, using spacecraft/channel identity, VC frame counters, first
header pointers, and science packet headers/sequences. It also detects stream
restarts inside each selected file. A split requires a discontinuous VC counter
reset to zero, a new FHP alignment at zero, a sequence-zero science packet with a
valid startup sync record, and at least three consistent frames in the new range.
Normal 255-to-zero counter wraparound and counter gaps alone do not cause a split.
Every resulting range is validated before it can be joined to other selected data.
The decoder orders unambiguous continuations and decodes each group as one input.
Multiple groups, or a single file containing several streams, can be selected.
Unsupported, damaged, unmatched, or ambiguous inputs are still attempted
individually. With the checkbox off, files are decoded individually without splitting.

For **TMTC**, the supported format is the existing decoder's 144-byte APID 145
packet (6-byte GICD header, 10-byte secondary header, 128-byte ICD data). Matching
validates packet headers/lengths, master/slave module IDs, ICD tails, the 14-bit
source sequence count, the 16-bit GTM packet counter, and GICD UTC. Both counters
must increment with normal wraparound; boundary timestamps must advance at the
same cadence as the adjacent packets on both sides. Known FS8B-style filenames
also constrain spacecraft identity and ordering. File timestamps alone are not
evidence of continuity. A partial packet at a file boundary is joined only when
its combined bytes validate as a complete packet with the expected counters/time.
At least two complete packets per initial range are needed to establish cadence.

TMTC can also split a selected file at a confirmed startup: source sequence 1,
GTM counter 3, unset ICD time, advancing GICD time, and at least three consistent
packets following the reset. Other gaps or uncertain resets retain individual
decoding. A normal counter wrap is never treated as a restart. This conservative
matching can leave valid data separate when the sampling cadence changes at a
file boundary or the file is too short to confirm continuity.

Each group's output is saved beside its first file in continuity order. Input stems
are joined without separators, retaining the existing decoder suffix; for example:
`FS8B_2026285064610FS8B_2026285074611.vc3_science_pipeline.csv`.
TMTC publishes all three CSVs under the combined `.dmp` prefix, for example
`FS8B_SPL_2026285064610_145FS8B_SPL_2026285074611_145.dmp_tmtc_all.csv`,
with corresponding `_tmtc_master.csv` and `_tmtc_slave.csv` files. The seven
provided Day 285 TMTC inputs form three pairs (020238/030239, 052512/062512,
064610/074611) and one independent file (011945).
When a source file is split, its stem receives `_part1`, `_part2`, etc. before the
stems are joined. For example, selecting the three Day 232 files produces:

- `FS8B_2026232013029FS8B_2026232023029_part1.vc3_science_pipeline.csv`
- `FS8B_2026232023029_part2FS8B_2026232033030.vc3_science_pipeline.csv`

Raw exports and plot names use the same combined prefix. Original inputs and any
existing manually merged input files are preserved; only the exact byte ranges are copied
to temporary inputs, which are removed after processing. Extracted/combined inputs
are fixed snapshots and are plotted once; original individual files retain the
existing update/polling behavior. Terminal messages identify each split's byte
offset (and VC3 frame number), the source ranges, and incomplete trailing packet bytes.
Those tails remain in the originals; they are not joined across a detected restart
or decoded as complete packets.

Matching is conservative: unconfirmed resets, missing files, and repeated data
can prevent a unique match. A chain beginning in the middle of an ICD packet
still needs the preceding data for the current native decoder to decode it.
Groups exceeding the native decoder's 1,174,405,120-byte read limit or having
conflicting/unsupported output names fall back to individual decoding. If staging
one part fails, all affected groups fall back to the original inputs once each,
so the same source is not decoded both whole and in part. Native
decoder failures are isolated from the GUI and reported after other inputs have
been attempted. Terminal output lists the grouping and any fallback reasons.

### Flow terminal output

Run the GUI from a terminal to see Flow **Update**, **Clone**, and **Push** reports.
Each report identifies the operation and transfer direction, then prints the refreshed
file-list or transfer logs, including file names and any recorded SFTP errors. Update
refreshes directory listings; Clone and Push report downloads and uploads respectively.
SOCC transfer details come from the logs retrieved from the Operation server. The same
reporting applies to the Archiving controls.

The GUI stays responsive while scripts run. Passwords are masked, and missing or
unchanged logs are explicitly identified instead of showing an old transfer as a new
result. The script exit code is reported separately because a script may exit zero
even when an individual SFTP command fails.

### MTL / CMD OBC selection and DAC defaults

- In **MTL / CMD > Command Line**, choose **OBCA** or **OBCB** before generating commands. The default is OBCA. Changing the selection after generation enables the command Generate button again.
- In **specific MTL**, each orbit row has its own OBC dropdown beside **CMD_config**. Every new row defaults to OBCA, independently of the main-page selection. The selected OBC applies to every ON and OFF command for that row, including all intervals produced by its orbit filters.
- Byte 119 is set to exactly `0x00` for OBCA or `0x03` for OBCB, matching the existing **BUS_A_NEW / BUS_B_NEW** procedures. Under ICD CDRL-1009 NSPO 240926_L, table 5-1, these values select PPS_A / PPS_B respectively while keeping SpaceWire in AUTO mode. Byte 125 is recalculated as the sum of bytes 2-124 modulo 256 after applying all settings. Byte numbers are zero-based.
- Channel DAC defaults come from **DAC_setup_with_FS8A_panel_temperature.xlsx**, **sheet1 column D** (`new DAC value hex for FS8A panel T`), matched by the channel names in column B. The four sets of 32 channels populate bytes 44-75 of the Master-A, Master-B, Slave-A and Slave-B configuration commands. Both ordinary generation and the specific MTL DAC editor use these defaults. Per-orbit edits remain available through **CMD_config**.

To run the command and GUI regression tests in the installed GUI environment:

```text
conda activate env_gtm
python -B -m unittest discover -s tests -v
```
