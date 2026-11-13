# Single-file suite format

A suite is a native executable with payload data appended (ELF, PE) or loaded
from a Mach-O section (macOS).  All integers are little-endian.

```
+--------------------------+
| launcher machine code    |
+--------------------------+
| group 0 (raw DEFLATE)    |
| group 1                  |
| ...                      |
+--------------------------+
| manifest (ASCII)         |
+--------------------------+
| 128-byte footer          |
+--------------------------+  <- end of file (append mode)
```

For the Mach-O path the region `[group...manifest][footer]` is stored in the
`__DATA,__jorek_suite` section and the offsets in the footer are relative to
the start of that section.  The launcher obtains it with `getsectiondata()`.
For ELF/PE it reads the footer at the end of its own file and computes
`base = filesize - footer_size - manifest_size - archive_size`; every file
offset is then `base + archive_offset + group_offset`.

## Footer

| Offset | Size | Meaning |
| --- | --- | --- |
| 0 | 16 | magic `JOREKSUITE0001\0\0` |
| 16 | 4 | format version (1) |
| 20 | 4 | footer size (128) |
| 24 | 8 | manifest offset |
| 32 | 8 | manifest size |
| 40 | 8 | archive offset (0 in Mach-O section mode) |
| 48 | 8 | archive size |
| 56 | 32 | SHA-256 of manifest |
| 88 | 32 | reserved, zero |
| 120 | 4 | CRC-32 of bytes 0..119 |
| 124 | 4 | reserved, zero |

The manifest checksum is verified before parsing.  Every group and program is
verified against its own SHA-256 before extraction.  A corrupted or truncated
suite produces a diagnostic, not a memory-safety bug.

## Manifest

```
format 1
suite-version 1.0.0
jorek-version <git describe>
build-id <hex>
default-variant <name>
group <id> <offset> <compressed-size> <uncompressed-size> <sha256>
variant <name> <model> <features> <params> <group>
program <variant> <name> <group> <offset> <size> <mode> <sha256>
helper <name> <group> <offset> <size> <mode> <sha256>
```

`helper` entries are executable support files packaged with the suite, such as
the bundled `mpiexec`/Hydra process manager.  They are not JOREK variants and
are omitted from `--list`.

`features` and `params` are comma-separated, with `-` for an empty list.
Names are restricted to `[A-Za-z0-9._+=-]` and may not contain path
separators; the launcher rejects unsafe names.

A group is a solid DEFLATE stream over the concatenation of its programs.
Program offsets are relative to the uncompressed group.  Solid compression is
important because JOREK executables share most of their code; grouping by
variant gives a good ratio without decompressing unrelated variants.

## Runtime selection

1. Explicit `--variant`, then `--model`, `--features`, `--params`.
2. `JOREK_SUITE_*` environment variables.
3. Busybox-style `argv[0]` (`jorek_model303`, `jorek2vtk`).
4. A leading `@jorek_model: N` comment or a `model = N` line on standard
   input, when standard input is not a terminal.
5. If exactly one variant exists, use it.

The launcher passes its standard input through unchanged.  If it had to read
standard input for inference, it writes it to the child through a pipe (POSIX)
or a temporary file (Windows), so JOREK still sees the complete namelist.

## Extraction and caching

The cache is `$JOREK_SUITE_CACHE`, else `$XDG_CACHE_HOME/jorek-suite`, else
`~/.cache/jorek-suite` (Linux/BSD), `~/Library/Caches/jorek-suite` (macOS), or
`%LOCALAPPDATA%\jorek-suite\cache` (Windows).  Files are written to a unique
temporary name, fsync'ed, and atomically renamed.  Concurrent MPI ranks can
therefore race harmlessly.  On a read-only home directory the launcher falls
back to the operating-system temporary directory.

`--ephemeral` / `--no-cache` extracts to a temporary file and removes it after
the child exits.  `--extract-only` prints the path and does not execute.
