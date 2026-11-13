#!/usr/bin/env bash
# Build the static solver stack used by the Linux full suite.
#
# This script is intentionally separate from build.py: the upstream PaStiX 5/6
# and STRUMPACK build systems need a few compatibility patches that are easy to
# audit in one place.  It installs into $PREFIX and leaves PaStiX 5 in
# $PREFIX/pastix5.
set -euo pipefail
PREFIX=${PREFIX:-$PWD/build-linux/solvers}
CORE_PREFIX=${CORE_PREFIX:-$PWD/build-linux/deps}
WORK=${WORK:-$PWD/build-linux/solver-src}
JOBS=${JOBS:-$(nproc 2>/dev/null || echo 4)}
CC=${CC:-gcc}
CXX=${CXX:-g++}
FC=${FC:-gfortran}
MPICC=${MPICC:-mpicc}
MPICXX=${MPICXX:-mpicxx}
MPIFC=${MPIFC:-mpifort}
METIS_VERSION=5.2.1
PASTIX_VERSION=6.3.2
STRUMPACK_VERSION=7.1.0
PASTIX5_VERSION=5.2.3
mkdir -p "$PREFIX" "$WORK"
cd "$WORK"

fetch() { # url output
  [ -s "$2" ] || curl -L --http1.1 --retry 5 --retry-delay 2 -o "$2" "$1"
}

# --- GKlib + METIS ---------------------------------------------------------
if [ ! -f "$PREFIX/lib/libmetis.a" ]; then
  fetch "https://github.com/KarypisLab/GKlib/archive/refs/heads/master.tar.gz" gklib.tar.gz
  rm -rf GKlib-master GKlib; tar xzf gklib.tar.gz; mv GKlib-master GKlib
  rm -rf GKlib/build
  cmake -S GKlib -B GKlib/build -DCMAKE_INSTALL_PREFIX="$PREFIX" -DSHARED=0 \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5
  cmake --build GKlib/build --parallel "$JOBS"
  cmake --install GKlib/build

  fetch "https://github.com/KarypisLab/METIS/archive/refs/tags/v${METIS_VERSION}.tar.gz" metis.tar.gz
  rm -rf "METIS-${METIS_VERSION}"; tar xzf metis.tar.gz
  cd "METIS-${METIS_VERSION}"
  rm -rf build; mkdir -p build/xinclude
  printf '#define IDXTYPEWIDTH 32\n#define REALTYPEWIDTH 32\n' > build/xinclude/metis.h
  cat include/metis.h >> build/xinclude/metis.h
  cp include/CMakeLists.txt build/xinclude/
  cmake -S . -B build -DCMAKE_INSTALL_PREFIX="$PREFIX" -DGKLIB_PATH="$PREFIX" \
        -DSHARED=0 -DCMAKE_POLICY_VERSION_MINIMUM=3.5
  cmake --build build --parallel "$JOBS"
  cmake --install build
  cd "$WORK"
fi

# A merged METIS+GKlib archive is needed by the old PaStiX 5 find module.
if [ ! -f "$PREFIX/lib/libmetis-merged.a" ]; then
  rm -rf metis-merge; mkdir -p metis-merge/metis metis-merge/gklib; cd metis-merge
  (cd metis && ar x "$PREFIX/lib/libmetis.a")
  (cd gklib && ar x "$PREFIX/lib/libGKlib.a")
  for f in metis/*.o gklib/*.o; do
    cp "$f" "m_$(basename "$f")"
  done
  ar rcs "$PREFIX/lib/libmetis-merged.a" m_*.o
  cp "$PREFIX/lib/libmetis.a" "$PREFIX/lib/libmetis-split.a"
  cp "$PREFIX/lib/libmetis-merged.a" "$PREFIX/lib/libmetis.a"
  cd "$WORK"
fi

# --- PaStiX 6 --------------------------------------------------------------
if [ ! -f "$PREFIX/lib/libpastix.a" ]; then
  fetch "https://gitlab.inria.fr/solverstack/pastix/-/archive/v${PASTIX_VERSION}/pastix-v${PASTIX_VERSION}.tar.gz" pastix6.tar.gz
  rm -rf "pastix-v${PASTIX_VERSION}"; tar xzf pastix6.tar.gz
  cd "pastix-v${PASTIX_VERSION}"
  if [ ! -f spm/CMakeLists.txt ]; then
    fetch "https://gitlab.inria.fr/solverstack/spm/-/archive/master/spm-master.tar.gz" "$WORK/spm.tar.gz"
    fetch "https://gitlab.inria.fr/solverstack/morse_cmake/-/archive/master/morse_cmake-master.tar.gz" "$WORK/morse.tar.gz"
    rm -rf spm cmake_modules/morse_cmake; mkdir -p spm cmake_modules
    tar xzf "$WORK/spm.tar.gz" -C "$WORK"; cp -a "$WORK/spm-master/." spm/
    tar xzf "$WORK/morse.tar.gz" -C "$WORK"; cp -a "$WORK/morse_cmake-master/." cmake_modules/morse_cmake/
  fi
  rm -rf build
  cmake -S . -B build -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DBUILD_SHARED_LIBS=OFF \
    -DPASTIX_WITH_MPI=ON -DPASTIX_WITH_FORTRAN=OFF -DSPM_BUILD_FORTRAN=OFF \
    -DSPM_WITH_FORTRAN=OFF -DPASTIX_INT64=OFF \
    -DPASTIX_ORDERING_METIS=ON -DPASTIX_ORDERING_SCOTCH=OFF \
    -DPASTIX_WITH_CUDA=OFF -DPASTIX_WITH_STARPU=OFF -DPASTIX_WITH_PARSEC=OFF \
    -DPASTIX_GENERATE_MODEL=OFF \
    -DCMAKE_C_COMPILER="$MPICC" -DCMAKE_Fortran_COMPILER="$MPIFC" \
    -DMETIS_LIBRARIES="$PREFIX/lib/libmetis.a" -DMETIS_INCLUDE_DIRS="$PREFIX/include" \
    -DCMAKE_PREFIX_PATH="$PREFIX;$CORE_PREFIX"
  cmake --build build --parallel "$JOBS"
  cmake --build build --target install
  cd "$WORK"
fi

# --- STRUMPACK -------------------------------------------------------------
if [ ! -f "$PREFIX/lib/libstrumpack.a" ]; then
  fetch "https://github.com/pghysels/STRUMPACK/archive/v${STRUMPACK_VERSION}.tar.gz" strumpack.tar.gz
  rm -rf "STRUMPACK-${STRUMPACK_VERSION}"; tar xzf strumpack.tar.gz
  cd "STRUMPACK-${STRUMPACK_VERSION}"
  rm -rf build
  cmake -S . -B build -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF -DSTRUMPACK_USE_MPI=ON -DSTRUMPACK_USE_OPENMP=ON \
    -DTPL_ENABLE_SLATE=OFF -DTPL_ENABLE_PARMETIS=OFF -DTPL_ENABLE_SCOTCH=OFF \
    -DTPL_ENABLE_PTSCOTCH=OFF -DTPL_ENABLE_BPACK=OFF -DTPL_ENABLE_ZFP=OFF \
    -DTPL_METIS_PREFIX="$PREFIX" \
    -DTPL_BLAS_LIBRARIES="$CORE_PREFIX/lib/libopenblas.a" \
    -DTPL_LAPACK_LIBRARIES="$CORE_PREFIX/lib/libopenblas.a" \
    -DTPL_SCALAPACK_LIBRARIES="$CORE_PREFIX/lib/libscalapack.a" \
    -DCMAKE_C_COMPILER="$CC" -DCMAKE_CXX_COMPILER="$CXX" -DCMAKE_Fortran_COMPILER="$FC" \
    -DCMAKE_PREFIX_PATH="$PREFIX;$CORE_PREFIX"
  cmake --build build --target strumpack --parallel "$JOBS"
  mkdir -p "$PREFIX/lib" "$PREFIX/include"
  cp build/libstrumpack.a "$PREFIX/lib/"
  cp build/StrumpackConfig.h "$PREFIX/include/"
  cp -a src/* "$PREFIX/include/"
  cd "$WORK"
fi

# --- PaStiX 5 (separate prefix; compile-time exclusive with 6) -------------
P5="$PREFIX/pastix5"
if [ ! -f "$P5/lib/libpastix.a" ]; then
  mkdir -p "$P5"
  fetch "http://sources.easybuild.io/p/PaStiX/pastix_${PASTIX5_VERSION}.tar.bz2" pastix5.tar.bz2
  rm -rf "pastix_${PASTIX5_VERSION}"; tar xjf pastix5.tar.bz2
  cd "pastix_${PASTIX5_VERSION}/src"
  [ -f sopalin/src/murge_fortran.c ] || printf 'int pastix_murge_dummy;\n' > sopalin/src/murge_fortran.c
  rm -rf build
  cmake -S . -B build -DCMAKE_INSTALL_PREFIX="$P5" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DBUILD_SHARED_LIBS=OFF \
    -DPASTIX_WITH_MPI=ON -DPASTIX_WITH_MULTITHREAD=ON -DPASTIX_INT64=OFF \
    -DPASTIX_ORDERING_METIS=ON -DPASTIX_ORDERING_SCOTCH=OFF \
    -DPASTIX_ORDERING_PTSCOTCH=OFF -DPASTIX_DISTRIBUTED=OFF \
    -DPASTIX_BUILD_EXAMPLES=OFF -DCMAKE_C_FLAGS="-Wno-implicit-function-declaration" \
    -DCMAKE_C_COMPILER="$MPICC" -DCMAKE_Fortran_COMPILER="$MPIFC" \
    -DMETIS_LIBRARIES="$PREFIX/lib/libmetis.a" -DMETIS_INCLUDE_DIRS="$PREFIX/include" \
    -DCMAKE_PREFIX_PATH="$PREFIX;$CORE_PREFIX"
  cmake --build build --target pastix --parallel "$JOBS"
  cmake --install build
  cd "$WORK"
fi
echo "solver prefix: $PREFIX"
