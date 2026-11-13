# Embedded runtime scripts

The suite embeds JOREK's **runtime** helper scripts, not the build system.
Build-only files such as `util/config.sh`, `util/makedepend`, `util/obj_deps`,
`util/compile_*.sh`, `util/launch_tests.sh`, and `util/equation_codegen/` are
excluded.

Included categories:

* all of `util/` except the build-only files above, including
  `plot_live_data.*`, `extract_live_data.sh`, `convert2vtk.sh`,
  `convert2poincare.sh`, `convert2CARIDDI.sh`, restart selectors, ADAS
  download helpers, `j2p`, `mem_monitor`, ParaView bindings, IMAS IDS scripts,
  and job templates;
* all of `run_jorek/` (`subjorek`, `substrikes`, `subconnection2`, namelists);
* runtime scheduler examples under `namelist/**/*.job` and
  `reg_tests/job_scripts/**/*.{job,sh}`;
* particle post-processing scripts under `particles/`;
* Catalyst and IMAS Python helpers under `diagnostics/` and `communication/`.

The reference suite reports:

```text
helpers: 174   # 6 MPI helpers + 168 runtime files
```

## Extract

```sh
jorek-suite --extract-helpers /shared/jorek-runtime
```

The extracted tree preserves repository-relative paths, e.g.
`/shared/jorek-runtime/util/plot_live_data.sh` and
`/shared/jorek-runtime/run_jorek/subjorek`.

## Run a script directly from the suite

```sh
jorek-suite --script util/analyze_logfile.sh -- jorek.log
jorek-suite --script util/plot_live_data.py -- --help
jorek-suite --script run_jorek/subjorek -- 303 1 1 4 input
```

`--script` extracts the script tree, sets `JOREK_RUNTIME_DIR`, and executes
the selected file with the arguments after `--`.  Scripts that read files from
the current directory still do so; run the command from your simulation
directory.

## Interpreter dependencies

The **JOREK solver itself is native** and needs no interpreter.  A runtime
script obviously needs its own interpreter and libraries:

| Script family | Needs |
| --- | --- |
| `.sh` | a POSIX shell (`/bin/sh` or bash) |
| `.py` | Python 3 and the scientific packages imported by that script (numpy, matplotlib, h5py, ...) |
| `.R` | R |
| `.gnuplot` | gnuplot |
| ParaView bindings | ParaView + Python |
| IMAS scripts | IMAS-Core and its Python bindings |
| `.job` templates | the corresponding batch scheduler |

Those interpreter/package dependencies are not bundled: embedding a Python
distribution, NumPy, matplotlib, gnuplot, R, ParaView, and every scheduler
client would dwarf the solver and is outside the native-solver contract.  The
scripts are embedded and ready to extract; their runtime environments remain
the user's responsibility.
