# Source acquisition policy

`build.py` has four dependency policies:

| `--deps` | Behaviour |
| --- | --- |
| `system` | use only static libraries found in `--dep-prefix` and `/usr/local`/`/usr`; fail if any required `.a` is missing |
| `source` | download/extract and build all core dependencies into `$WORK/deps` |
| `auto` (default) | use the system prefix if every required static library is present; otherwise build the missing stack from source |
| `none` | use only the prefixes supplied with `--dep-prefix` |

## Per-dependency source directory

```sh
python3 build.py full \
  --source hdf5=/data/src/hdf5-1.14.6 \
  --source mpich=/data/src/mpich-4.3.0 \
  --source openblas=/data/src/OpenBLAS-0.3.30
```

The directory is used as the extracted source root; no archive is downloaded.

A single tree containing conventionally named directories also works:

```sh
python3 build.py full --source-dir /data/jorek-sources
# looks for /data/jorek-sources/<name>-<version> or /data/jorek-sources/<name>
```

`--source-dir` is a convenience spelling; `--source NAME=DIR` is explicit and
wins for that dependency.

## Per-dependency URLs and mirrors

```sh
python3 build.py full \
  --url hdf5=https://mirror.example/hdf5-1.14.6.tar.gz \
  --mirror hdf5=https://mirror2.example/hdf5-1.14.6.tar.gz \
  --mirror mpich=https://mirror.example/mpich-4.3.0.tar.gz
```

`--url NAME=URL` and `--mirror NAME=URL` are both tried before the built-in
URL.  Repeating `--mirror` adds more fallbacks.  The downloader also retries
with `curl --http1.1` if Python's TLS stack fails.

## System static libraries

Static libraries are searched under:

```text
<prefix>/lib
<prefix>/lib64
<prefix>/lib/x86_64-linux-gnu
<prefix>/lib/aarch64-linux-gnu
```

For a complete system prefix:

```sh
python3 build.py full --deps system --dep-prefix /opt/jorek-static
```

## Static glibc

Static glibc is **not** just another library in this policy.  Building
`libc.a` from glibc source is possible, but the compiler must also use that
glibc's startup objects and target headers.  A `libc.a` dropped into a normal
toolchain is not a reliable static distribution.

Options:

* `--no-full-static`: static third-party libraries + dynamic system glibc
  (the practical Linux path);
* provide a complete static-libc toolchain/sysroot and pass its prefix with
  `--toolchain-prefix`;
* build inside a musl or static-glibc distribution.

The current builder does not silently pretend that a glibc source tarball is
a complete static toolchain.

## Solver stack

`src/solvers.py` builds PaStiX 5/6, STRUMPACK, METIS/GKlib and hwloc with
pinned upstream URLs.  The core `--url`/`--mirror` overrides above apply to
the `deps.py` dependency graph; the solver stack currently has its own pinned
sources.
