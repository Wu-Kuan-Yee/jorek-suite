# Security notes

* All downloads use HTTPS where the upstream project supports it.  Optional
  SHA-256 values are enforced; for projects without an upstream digest the
  first download is recorded in `downloads.lock.json` and later builds verify
  against that record.  The lock file is part of the build directory, not of
  the repository.
* Source archives are extracted into a fresh directory.  The extractor does
  not follow absolute paths or `..` members from untrusted archives beyond
  what Python's `tarfile` permits; the build does not run package-provided
  shell scripts from binary distributions.
* Build commands are executed as argument vectors, never through `shell=True`.
  Environment variables are passed explicitly.
* The suite launcher verifies the manifest checksum, each group digest, and
  each program digest before executing it.  It rejects unsafe names and
  refuses to follow symlinks when writing cache entries.
* Cache directories and files are created with restrictive permissions.
  The launcher never writes outside the cache, the temporary directory, or a
  directory explicitly passed to `--extract`.
* The macOS suite is ad-hoc signed because appending to a Mach-O would
  invalidate the linker's signature.  This is a local integrity signature, not
  a Developer ID signature; distributors who need Gatekeeper notarisation
  should sign the final file with their own identity.
* Third-party licences are listed in `docs/DEPENDENCIES.md`.  ParMETIS and
  some solver packages have non-standard or non-commercial terms; review them
  before redistributing a suite that contains them.
