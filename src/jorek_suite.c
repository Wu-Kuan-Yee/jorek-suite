/*
 * jorek_suite.c -- portable single-file launcher for JOREK build variants.
 *
 * A suite file is an ordinary native executable followed by an archive and a
 * small manifest.  The launcher reads its own tail, selects one of the
 * embedded JOREK programs, verifies its SHA-256 digest, materialises it in a
 * per-user cache (or a temporary directory), and executes it.
 *
 * The file format is deliberately simple: no dynamic libraries, no runtime
 * installation, no external decompressor.  Raw DEFLATE streams are decoded
 * with the public-domain puff.c implementation vendored next to this file.
 *
 * Copyright (c) 2026 JOREK build-jorek contributors.
 * SPDX-License-Identifier: LGPL-3.0-or-later
 */

#define _FILE_OFFSET_BITS 64
#define _LARGEFILE_SOURCE 1
#define _CRT_SECURE_NO_WARNINGS 1

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdarg.h>
#include <ctype.h>
#include <errno.h>
#include <time.h>
#include <limits.h>
#include <sys/types.h>
#include <sys/stat.h>

#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN 1
#  include <windows.h>
#  include <io.h>
#  include <direct.h>
#  include <process.h>
#  include <fcntl.h>
#  define JS_SEP '\\'
#  define strtok_r strtok_s
#  define js_getpid _getpid
#  define js_stat _stat64
#  define js_fseek _fseeki64
#  define js_ftell _ftelli64
#else
#  include <unistd.h>
#  include <fcntl.h>
#  include <sys/wait.h>
#  include <sys/time.h>
#  ifdef __APPLE__
#    include <mach-o/dyld.h>
#    include <mach-o/getsect.h>
#    include <mach-o/ldsyms.h>
#  endif
#  define JS_SEP '/'
#  define js_getpid getpid
#  define js_stat stat
#  define js_fseek fseeko
#  define js_ftell ftello
#endif

#include "puff.h"

#ifndef PATH_MAX
#  define PATH_MAX 4096
#endif

#define JS_VERSION "1.0.0"
#define JS_FORMAT_VERSION 1u
#define JS_FOOTER_SIZE 128u
#define JS_FOOTER_MAGIC "JOREKSUITE0001\0"
#define JS_MAGIC_LEN 16u
#define JS_MAX_INPUT (64ull * 1024ull * 1024ull)
#define JS_CACHE_LOCK_TIMEOUT 60

/* ------------------------------------------------------------------------- */
/* SHA-256, after the public-domain reference implementation by Brad Conte.  */
/* ------------------------------------------------------------------------- */
typedef struct {
    uint8_t data[64];
    uint32_t datalen;
    uint64_t bitlen;
    uint32_t state[8];
} SHA256_CTX;

#define ROTRIGHT(a, b) (((a) >> (b)) | ((a) << (32 - (b))))
#define CH(x, y, z) (((x) & (y)) ^ (~(x) & (z)))
#define MAJ(x, y, z) (((x) & (y)) ^ ((x) & (z)) ^ ((y) & (z)))
#define EP0(x) (ROTRIGHT(x, 2) ^ ROTRIGHT(x, 13) ^ ROTRIGHT(x, 22))
#define EP1(x) (ROTRIGHT(x, 6) ^ ROTRIGHT(x, 11) ^ ROTRIGHT(x, 25))
#define SIG0(x) (ROTRIGHT(x, 7) ^ ROTRIGHT(x, 18) ^ ((x) >> 3))
#define SIG1(x) (ROTRIGHT(x, 17) ^ ROTRIGHT(x, 19) ^ ((x) >> 10))

static const uint32_t js_k[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
    0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
    0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
    0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
    0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u, 0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u
};

static void sha256_transform(SHA256_CTX *ctx, const uint8_t data[]) {
    uint32_t a, b, c, d, e, f, g, h, i, j, t1, t2, m[64];
    for (i = 0, j = 0; i < 16; ++i, j += 4)
        m[i] = ((uint32_t)data[j] << 24) | ((uint32_t)data[j + 1] << 16) |
               ((uint32_t)data[j + 2] << 8) | ((uint32_t)data[j + 3]);
    for (; i < 64; ++i)
        m[i] = SIG1(m[i - 2]) + m[i - 7] + SIG0(m[i - 15]) + m[i - 16];
    a = ctx->state[0]; b = ctx->state[1]; c = ctx->state[2]; d = ctx->state[3];
    e = ctx->state[4]; f = ctx->state[5]; g = ctx->state[6]; h = ctx->state[7];
    for (i = 0; i < 64; ++i) {
        t1 = h + EP1(e) + CH(e, f, g) + js_k[i] + m[i];
        t2 = EP0(a) + MAJ(a, b, c);
        h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    ctx->state[0] += a; ctx->state[1] += b; ctx->state[2] += c; ctx->state[3] += d;
    ctx->state[4] += e; ctx->state[5] += f; ctx->state[6] += g; ctx->state[7] += h;
}

static void sha256_init(SHA256_CTX *ctx) {
    ctx->datalen = 0;
    ctx->bitlen = 0;
    ctx->state[0] = 0x6a09e667u; ctx->state[1] = 0xbb67ae85u;
    ctx->state[2] = 0x3c6ef372u; ctx->state[3] = 0xa54ff53au;
    ctx->state[4] = 0x510e527fu; ctx->state[5] = 0x9b05688cu;
    ctx->state[6] = 0x1f83d9abu; ctx->state[7] = 0x5be0cd19u;
}

static void sha256_update(SHA256_CTX *ctx, const uint8_t *data, size_t len) {
    size_t i;
    for (i = 0; i < len; ++i) {
        ctx->data[ctx->datalen++] = data[i];
        if (ctx->datalen == 64) {
            sha256_transform(ctx, ctx->data);
            ctx->bitlen += 512;
            ctx->datalen = 0;
        }
    }
}

static void sha256_final(SHA256_CTX *ctx, uint8_t hash[32]) {
    uint32_t i = ctx->datalen;
    if (ctx->datalen < 56) {
        ctx->data[i++] = 0x80;
        while (i < 56) ctx->data[i++] = 0x00;
    } else {
        ctx->data[i++] = 0x80;
        while (i < 64) ctx->data[i++] = 0x00;
        sha256_transform(ctx, ctx->data);
        memset(ctx->data, 0, 56);
    }
    ctx->bitlen += (uint64_t)ctx->datalen * 8;
    ctx->data[63] = (uint8_t)(ctx->bitlen);
    ctx->data[62] = (uint8_t)(ctx->bitlen >> 8);
    ctx->data[61] = (uint8_t)(ctx->bitlen >> 16);
    ctx->data[60] = (uint8_t)(ctx->bitlen >> 24);
    ctx->data[59] = (uint8_t)(ctx->bitlen >> 32);
    ctx->data[58] = (uint8_t)(ctx->bitlen >> 40);
    ctx->data[57] = (uint8_t)(ctx->bitlen >> 48);
    ctx->data[56] = (uint8_t)(ctx->bitlen >> 56);
    sha256_transform(ctx, ctx->data);
    for (i = 0; i < 4; ++i) {
        hash[i]      = (uint8_t)((ctx->state[0] >> (24 - i * 8)) & 0xff);
        hash[i + 4]  = (uint8_t)((ctx->state[1] >> (24 - i * 8)) & 0xff);
        hash[i + 8]  = (uint8_t)((ctx->state[2] >> (24 - i * 8)) & 0xff);
        hash[i + 12] = (uint8_t)((ctx->state[3] >> (24 - i * 8)) & 0xff);
        hash[i + 16] = (uint8_t)((ctx->state[4] >> (24 - i * 8)) & 0xff);
        hash[i + 20] = (uint8_t)((ctx->state[5] >> (24 - i * 8)) & 0xff);
        hash[i + 24] = (uint8_t)((ctx->state[6] >> (24 - i * 8)) & 0xff);
        hash[i + 28] = (uint8_t)((ctx->state[7] >> (24 - i * 8)) & 0xff);
    }
}

static void sha256_bytes(const void *buf, size_t len, uint8_t out[32]) {
    SHA256_CTX ctx;
    sha256_init(&ctx);
    sha256_update(&ctx, (const uint8_t *)buf, len);
    sha256_final(&ctx, out);
}

static void hex32(const uint8_t in[32], char out[65]) {
    static const char *d = "0123456789abcdef";
    int i;
    for (i = 0; i < 32; ++i) {
        out[i * 2] = d[in[i] >> 4];
        out[i * 2 + 1] = d[in[i] & 15];
    }
    out[64] = '\0';
}

static int hex_nibble(int c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int parse_hex32(const char *s, uint8_t out[32]) {
    int i;
    if (!s || strlen(s) < 64) return -1;
    for (i = 0; i < 32; ++i) {
        int hi = hex_nibble((unsigned char)s[i * 2]);
        int lo = hex_nibble((unsigned char)s[i * 2 + 1]);
        if (hi < 0 || lo < 0) return -1;
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    return 0;
}

/* ------------------------------------------------------------------------- */
/* Small utilities                                                          */
/* ------------------------------------------------------------------------- */
static void *js_malloc(size_t n) {
    void *p = malloc(n ? n : 1);
    if (!p) {
        fprintf(stderr, "jorek-suite: out of memory (%lu bytes)\n", (unsigned long)n);
        exit(4);
    }
    return p;
}

static void *js_calloc(size_t n, size_t s) {
    void *p = calloc(n ? n : 1, s ? s : 1);
    if (!p) {
        fprintf(stderr, "jorek-suite: out of memory\n");
        exit(4);
    }
    return p;
}

static void *js_realloc(void *p, size_t n) {
    void *q = realloc(p, n ? n : 1);
    if (!q) {
        fprintf(stderr, "jorek-suite: out of memory (%lu bytes)\n", (unsigned long)n);
        exit(4);
    }
    return q;
}

static char *js_strdup(const char *s) {
    size_t n;
    char *p;
    if (!s) s = "";
    n = strlen(s) + 1;
    p = (char *)js_malloc(n);
    memcpy(p, s, n);
    return p;
}

static char *js_strndup(const char *s, size_t n) {
    char *p = (char *)js_malloc(n + 1);
    memcpy(p, s, n);
    p[n] = '\0';
    return p;
}

static int js_streq(const char *a, const char *b) {
    return a && b && strcmp(a, b) == 0;
}


static int js_is_safe_name(const char *s) {
    const unsigned char *p = (const unsigned char *)s;
    if (!s || !*s) return 0;
    if (s[0] == '.' && (s[1] == '\0' || (s[1] == '.' && s[2] == '\0'))) return 0;
    for (; *p; ++p) {
        if (!(isalnum(*p) || *p == '_' || *p == '-' || *p == '.' || *p == '+' || *p == '=')) return 0;
    }
    return 1;
}

#ifdef _WIN32
static void js_strlcpy(char *dst, const char *src, size_t size) {
    if (size == 0) return;
    strncpy(dst, src, size - 1);
    dst[size - 1] = '\0';
}
#endif

static char *js_path_join(const char *a, const char *b) {
    size_t na = strlen(a), nb = strlen(b);
    int need_sep = na > 0 && a[na - 1] != '/' && a[na - 1] != '\\';
    char *p = (char *)js_malloc(na + nb + 2);
    memcpy(p, a, na);
    if (need_sep) p[na++] = JS_SEP;
    memcpy(p + na, b, nb + 1);
    return p;
}

#ifdef _WIN32
static wchar_t *js_utf8_to_wide(const char *s) {
    int n = MultiByteToWideChar(CP_UTF8, 0, s, -1, NULL, 0);
    wchar_t *w;
    if (n <= 0) return NULL;
    w = (wchar_t *)js_malloc((size_t)n * sizeof(wchar_t));
    MultiByteToWideChar(CP_UTF8, 0, s, -1, w, n);
    return w;
}

static FILE *js_fopen(const char *path, const char *mode) {
    wchar_t *wp = js_utf8_to_wide(path);
    wchar_t wmode[8];
    FILE *f;
    if (!wp) return NULL;
    MultiByteToWideChar(CP_UTF8, 0, mode, -1, wmode, 8);
    f = _wfopen(wp, wmode);
    free(wp);
    return f;
}

static int js_mkdir_one(const char *path) {
    wchar_t *w = js_utf8_to_wide(path);
    int rc;
    if (!w) return -1;
    rc = _wmkdir(w);
    free(w);
    return rc;
}
#else
static FILE *js_fopen(const char *path, const char *mode) { return fopen(path, mode); }
static int js_mkdir_one(const char *path) { return mkdir(path, 0700); }
#endif

static int js_mkdir_p(const char *path) {
    char *tmp;
    size_t i, n;
    int rc = 0;
    if (!path || !*path) return -1;
    tmp = js_strdup(path);
    n = strlen(tmp);
    for (i = 1; i < n; ++i) {
        if (tmp[i] == '/' || tmp[i] == '\\') {
            char save = tmp[i];
            tmp[i] = '\0';
#ifdef _WIN32
            if (i == 2 && tmp[1] == ':') { tmp[i] = save; continue; }
#endif
            if (tmp[0] && js_mkdir_one(tmp) != 0 && errno != EEXIST) { rc = -1; }
            tmp[i] = save;
        }
    }
    if (js_mkdir_one(tmp) != 0 && errno != EEXIST) rc = -1;
    free(tmp);
    return rc;
}

static int js_file_size(const char *path, uint64_t *size) {
    struct js_stat st;
    if (js_stat(path, &st) != 0) return -1;
    *size = (uint64_t)st.st_size;
    return 0;
}

static int js_read_file_at(FILE *f, uint64_t off, void *buf, size_t len) {
    if (js_fseek(f, (long long)off, SEEK_SET) != 0) return -1;
    if (len == 0) return 0;
    return fread(buf, 1, len, f) == len ? 0 : -1;
}



/* ------------------------------------------------------------------------- */
/* Command line / diagnostics                                               */
/* ------------------------------------------------------------------------- */
static int g_verbose = 0;
static int g_quiet = 0;
static const char *g_cache_override = NULL;

#ifdef _WIN32
static int js_setenv(const char *name, const char *value, int overwrite) {
    (void)overwrite;
    return _putenv_s(name, value);
}
#else
static int js_setenv(const char *name, const char *value, int overwrite) {
    return setenv(name, value, overwrite);
}
#endif

static void js_vlog(const char *fmt, va_list ap) {
    if (g_quiet) return;
    vfprintf(stdout, fmt, ap);
    fputc('\n', stdout);
    fflush(stdout);
}

static void js_log(const char *fmt, ...) {
    va_list ap;
    if (!g_verbose) return;
    va_start(ap, fmt);
    js_vlog(fmt, ap);
    va_end(ap);
}


static void js_warn(const char *fmt, ...) {
    va_list ap;
    fprintf(stderr, "jorek-suite: warning: ");
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
}

static void js_die(int code, const char *fmt, ...) {
    va_list ap;
    fprintf(stderr, "jorek-suite: error: ");
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    exit(code);
}

/* ------------------------------------------------------------------------- */
/* Manifest                                                                 */
/* ------------------------------------------------------------------------- */
typedef struct {
    char *name;
    char *model;
    char *features;
    char *params;
    int group;
} Variant;

typedef struct {
    char *variant;
    char *name;
    int group;
    uint64_t off;
    uint64_t size;
    unsigned mode;
    uint8_t sha[32];
} Program;

typedef struct {
    int id;
    uint64_t off;
    uint64_t csize;
    uint64_t usize;
    uint8_t sha[32];
} Group;

typedef struct {
    char *path;
    FILE *fp;
    const unsigned char *blob;
    uint64_t blob_size;
    int blob_memory;
    uint64_t file_size;
    uint64_t archive_off;
    uint64_t manifest_off;
    uint64_t manifest_size;
    uint8_t manifest_sha[32];
    char *manifest;
    char *suite_version;
    char *jorek_version;
    char *build_id;
    char *default_variant;
    Variant *variants;
    size_t nvariants;
    Program *programs;
    size_t nprograms;
    Group *groups;
    size_t ngroups;
} Suite;

static int js_read_at(Suite *s, uint64_t off, void *buf, size_t len) {
    if (s->blob_memory) {
        if (off > s->blob_size || len > s->blob_size - off) return -1;
        memcpy(buf, s->blob + off, len);
        return 0;
    }
    return js_read_file_at(s->fp, off, buf, len);
}


static int js_add_variant(Suite *s, const char *name, const char *model,
                          const char *features, const char *params, int group) {
    Variant *v;
    if (!js_is_safe_name(name) || !js_is_safe_name(model) || group < 0) return -1;
    s->variants = (Variant *)js_realloc(s->variants, (s->nvariants + 1) * sizeof(Variant));
    v = &s->variants[s->nvariants++];
    memset(v, 0, sizeof(*v));
    v->name = js_strdup(name);
    v->model = js_strdup(model);
    v->features = js_strdup(features);
    v->params = js_strdup(params);
    v->group = group;
    return 0;
}

static int js_add_program(Suite *s, const char *variant, const char *name,
                          int group, uint64_t off, uint64_t size, unsigned mode,
                          const char *sha_hex) {
    Program *p;
    if (!js_is_safe_name(variant) || !js_is_safe_name(name) || group < 0) return -1;
    s->programs = (Program *)js_realloc(s->programs, (s->nprograms + 1) * sizeof(Program));
    p = &s->programs[s->nprograms++];
    memset(p, 0, sizeof(*p));
    p->variant = js_strdup(variant);
    p->name = js_strdup(name);
    p->group = group;
    p->off = off;
    p->size = size;
    p->mode = mode;
    if (parse_hex32(sha_hex, p->sha) != 0) return -1;
    return 0;
}

static int js_add_group(Suite *s, int id, uint64_t off, uint64_t csize,
                        uint64_t usize, const char *sha_hex) {
    Group *g;
    s->groups = (Group *)js_realloc(s->groups, (s->ngroups + 1) * sizeof(Group));
    g = &s->groups[s->ngroups++];
    memset(g, 0, sizeof(*g));
    g->id = id;
    g->off = off;
    g->csize = csize;
    g->usize = usize;
    if (parse_hex32(sha_hex, g->sha) != 0) return -1;
    return 0;
}

static int js_parse_manifest(Suite *s) {
    char *copy = js_strdup(s->manifest);
    char *save_line = NULL;
    char *line;
    int lineno = 0;
    for (line = strtok_r(copy, "\n", &save_line); line; line = strtok_r(NULL, "\n", &save_line)) {
        char *save_tok = NULL;
        char *tok;
        char *fields[10];
        int nf = 0;
        lineno++;
        while (*line == ' ' || *line == '\t' || *line == '\r') line++;
        if (!*line || *line == '#') continue;
        tok = strtok_r(line, " \t\r", &save_tok);
        while (tok && nf < 10) {
            fields[nf++] = tok;
            tok = strtok_r(NULL, " \t\r", &save_tok);
        }
        if (nf == 0) continue;
        if (js_streq(fields[0], "format")) {
            if (nf < 2 || atoi(fields[1]) != 1) js_die(3, "unsupported manifest format on line %d", lineno);
        } else if (js_streq(fields[0], "suite-version")) {
            if (nf >= 2) { free(s->suite_version); s->suite_version = js_strdup(fields[1]); }
        } else if (js_streq(fields[0], "jorek-version")) {
            if (nf >= 2) { free(s->jorek_version); s->jorek_version = js_strdup(fields[1]); }
        } else if (js_streq(fields[0], "build-id")) {
            if (nf >= 2) { free(s->build_id); s->build_id = js_strdup(fields[1]); }
        } else if (js_streq(fields[0], "default-variant")) {
            if (nf >= 2) { free(s->default_variant); s->default_variant = js_strdup(fields[1]); }
        } else if (js_streq(fields[0], "variant")) {
            if (nf < 6) js_die(3, "malformed variant on line %d", lineno);
            if (js_add_variant(s, fields[1], fields[2], fields[3], fields[4], atoi(fields[5])) != 0)
                js_die(3, "invalid variant name on line %d", lineno);
        } else if (js_streq(fields[0], "program")) {
            if (nf < 8) js_die(3, "malformed program on line %d", lineno);
            if (js_add_program(s, fields[1], fields[2], atoi(fields[3]),
                               (uint64_t)strtoull(fields[4], NULL, 10),
                               (uint64_t)strtoull(fields[5], NULL, 10),
                               (unsigned)strtoul(fields[6], NULL, 8), fields[7]) != 0)
                js_die(3, "invalid digest on line %d", lineno);
        } else if (js_streq(fields[0], "group")) {
            if (nf < 6) js_die(3, "malformed group on line %d", lineno);
            if (js_add_group(s, atoi(fields[1]),
                             (uint64_t)strtoull(fields[2], NULL, 10),
                             (uint64_t)strtoull(fields[3], NULL, 10),
                             (uint64_t)strtoull(fields[4], NULL, 10), fields[5]) != 0)
                js_die(3, "invalid group digest on line %d", lineno);
        } else if (js_streq(fields[0], "meta") || js_streq(fields[0], "description")) {
            /* reserved for forward-compatible metadata */
        } else {
            js_warn("ignoring unknown manifest key '%s' on line %d", fields[0], lineno);
        }
    }
    free(copy);
    if (!s->suite_version) s->suite_version = js_strdup(JS_VERSION);
    if (!s->jorek_version) s->jorek_version = js_strdup("unknown");
    if (!s->build_id) s->build_id = js_strdup("unknown");
    if (!s->default_variant) s->default_variant = js_strdup("");
    return 0;
}

static int js_load_suite(const char *path, Suite *s) {
    unsigned char footer[JS_FOOTER_SIZE];
    uint32_t fmt, crc, i;
    uint64_t total;
    char *buf;
    memset(s, 0, sizeof(*s));
    s->path = js_strdup(path);
#ifdef __APPLE__
    {
        unsigned long secsize = 0;
        const unsigned char *sec = getsectiondata(&_mh_execute_header, "__DATA", "__jorek_suite", &secsize);
        if (sec && secsize >= JS_FOOTER_SIZE) {
            s->blob = sec;
            s->blob_size = (uint64_t)secsize;
            s->blob_memory = 1;
            total = (uint64_t)secsize;
        }
    }
#endif
    if (!s->blob_memory) {
        s->fp = js_fopen(path, "rb");
        if (!s->fp) js_die(3, "cannot open suite '%s': %s", path, strerror(errno));
        if (js_file_size(path, &total) != 0 || total < JS_FOOTER_SIZE)
            js_die(3, "'%s' is not a JOREK suite (too short)", path);
    }
    if (js_read_at(s, total - JS_FOOTER_SIZE, footer, JS_FOOTER_SIZE) != 0)
        js_die(3, "cannot read suite footer");
    if (memcmp(footer, JS_FOOTER_MAGIC, JS_MAGIC_LEN) != 0)
        js_die(3, "'%s' is not a JOREK suite (missing footer magic)", path);
    fmt = (uint32_t)footer[16] | ((uint32_t)footer[17] << 8) |
          ((uint32_t)footer[18] << 16) | ((uint32_t)footer[19] << 24);
    if (fmt != JS_FORMAT_VERSION)
        js_die(3, "unsupported suite format %u (expected %u)", fmt, JS_FORMAT_VERSION);
    /* CRC-32 over the first 124 footer bytes. */
    crc = 0xffffffffu;
    for (i = 0; i < 120; ++i) {
        int k;
        crc ^= footer[i];
        for (k = 0; k < 8; ++k) crc = (crc >> 1) ^ (0xedb88320u & (uint32_t)(-(int32_t)(crc & 1)));
    }
    crc ^= 0xffffffffu;
    {
        uint32_t want = (uint32_t)footer[120] | ((uint32_t)footer[121] << 8) |
                        ((uint32_t)footer[122] << 16) | ((uint32_t)footer[123] << 24);
        if (crc != want) js_die(3, "suite footer checksum mismatch");
    }
    s->manifest_off = (uint64_t)footer[24] | ((uint64_t)footer[25] << 8) |
                      ((uint64_t)footer[26] << 16) | ((uint64_t)footer[27] << 24) |
                      ((uint64_t)footer[28] << 32) | ((uint64_t)footer[29] << 40) |
                      ((uint64_t)footer[30] << 48) | ((uint64_t)footer[31] << 56);
    s->manifest_size = (uint64_t)footer[32] | ((uint64_t)footer[33] << 8) |
                       ((uint64_t)footer[34] << 16) | ((uint64_t)footer[35] << 24) |
                       ((uint64_t)footer[36] << 32) | ((uint64_t)footer[37] << 40) |
                       ((uint64_t)footer[38] << 48) | ((uint64_t)footer[39] << 56);
    s->archive_off = (uint64_t)footer[40] | ((uint64_t)footer[41] << 8) |
                     ((uint64_t)footer[42] << 16) | ((uint64_t)footer[43] << 24) |
                     ((uint64_t)footer[44] << 32) | ((uint64_t)footer[45] << 40) |
                     ((uint64_t)footer[46] << 48) | ((uint64_t)footer[47] << 56);
    memcpy(s->manifest_sha, footer + 56, 32);
    if (s->manifest_off > total || s->manifest_size > total - s->manifest_off ||
        s->manifest_size == 0 || s->manifest_size > 64ull * 1024ull * 1024ull)
        js_die(3, "invalid manifest bounds in suite footer");
    s->file_size = total;
    buf = (char *)js_malloc((size_t)s->manifest_size + 1);
    if (js_read_at(s, s->manifest_off, buf, (size_t)s->manifest_size) != 0)
        js_die(3, "cannot read suite manifest");
    buf[s->manifest_size] = '\0';
    s->manifest = buf;
    {
        uint8_t got[32];
        sha256_bytes(buf, (size_t)s->manifest_size, got);
        if (memcmp(got, s->manifest_sha, 32) != 0)
            js_die(3, "suite manifest checksum mismatch");
    }
    js_parse_manifest(s);
    {
        size_t gi;
        for (gi = 0; gi < s->ngroups; ++gi) {
            Group *g = &s->groups[gi];
            if (g->off > s->file_size || g->csize > s->file_size ||
                s->archive_off > s->file_size ||
                g->off > s->file_size - s->archive_off ||
                g->csize > s->file_size - s->archive_off - g->off)
                js_die(3, "group %d lies outside the suite archive", g->id);
        }
    }
    if (s->nvariants == 0 && s->nprograms == 0)
        js_die(3, "suite manifest is empty");
    return 0;
}

static Variant *js_find_variant(Suite *s, const char *name) {
    size_t i;
    for (i = 0; i < s->nvariants; ++i)
        if (js_streq(s->variants[i].name, name)) return &s->variants[i];
    return NULL;
}

static Group *js_find_group(Suite *s, int id) {
    size_t i;
    for (i = 0; i < s->ngroups; ++i)
        if (s->groups[i].id == id) return &s->groups[i];
    return NULL;
}

static Program *js_find_program(Suite *s, Variant *v, const char *name) {
    size_t i;
    if (!name || !*name) return NULL;
    for (i = 0; i < s->nprograms; ++i) {
        Program *p = &s->programs[i];
        if (js_streq(p->variant, v->name) && js_streq(p->name, name)) return p;
    }
    return NULL;
}

static Program *js_default_program(Suite *s, Variant *v) {
    size_t i;
    for (i = 0; i < s->nprograms; ++i)
        if (js_streq(s->programs[i].variant, v->name) && js_streq(s->programs[i].name, "jorek2_main"))
            return &s->programs[i];
    for (i = 0; i < s->nprograms; ++i)
        if (js_streq(s->programs[i].variant, v->name)) return &s->programs[i];
    return NULL;
}

static int js_csv_contains(const char *csv, const char *item) {
    const char *p = csv;
    size_t n = strlen(item);
    while (p && *p) {
        const char *comma = strchr(p, ',');
        size_t len = comma ? (size_t)(comma - p) : strlen(p);
        if (len == n && strncmp(p, item, n) == 0) return 1;
        if (!comma) break;
        p = comma + 1;
    }
    return 0;
}

/* Every requested feature/parameter must be present. */
static int js_csv_subset(const char *have, const char *want) {
    const char *p = want;
    if (!want || !*want) return 1;
    while (p && *p) {
        const char *comma = strchr(p, ',');
        size_t len = comma ? (size_t)(comma - p) : strlen(p);
        char *item;
        int ok;
        if (len == 0) { if (!comma) break; p = comma + 1; continue; }
        item = js_strndup(p, len);
        ok = js_csv_contains(have, item);
        free(item);
        if (!ok) return 0;
        if (!comma) break;
        p = comma + 1;
    }
    return 1;
}

static int js_feature_penalty(const char *have, const char *want) {
    /* Count how many extra features a candidate carries. */
    int n = 0;
    const char *p = have;
    while (p && *p) {
        const char *comma = strchr(p, ',');
        size_t len = comma ? (size_t)(comma - p) : strlen(p);
        char *item = js_strndup(p, len);
        if (*item && !js_csv_contains(want, item)) n++;
        free(item);
        if (!comma) break;
        p = comma + 1;
    }
    return n;
}

static int js_normalize_model(const char *in, char *out, size_t outsz) {
    const char *p = in;
    size_t n;
    if (!in || !*in) return -1;
    if (strncmp(p, "model", 5) == 0) p += 5;
    if (!*p) return -1;
    if (!isdigit((unsigned char)*p)) return -1;
    n = strlen(p);
    if (n >= outsz) return -1;
    strcpy(out, p);
    return 0;
}

/* ------------------------------------------------------------------------- */
/* Variant selection                                                        */
/* ------------------------------------------------------------------------- */
static int js_variant_has_program(Suite *s, Variant *v, const char *name) {
    return js_find_program(s, v, name) != NULL;
}

static Variant *js_select_variant(Suite *s, const char *variant_name,
                                  const char *model, const char *features,
                                  const char *params, const char *program,
                                  int allow_ambiguous, char **why) {
    Variant *best = NULL;
    int best_penalty = INT_MAX;
    size_t i;
    char norm[64];
    const char *want_model = model;
    if (why) *why = NULL;
    if (variant_name && *variant_name) {
        best = js_find_variant(s, variant_name);
        if (!best) {
            if (why) *why = js_strdup("no variant with that name");
            return NULL;
        }
        if (program && *program && !js_variant_has_program(s, best, program)) {
            if (why) *why = js_strdup("selected variant does not contain the requested program");
            return NULL;
        }
        return best;
    }
    if (model && *model) {
        if (js_normalize_model(model, norm, sizeof(norm)) != 0) {
            if (why) *why = js_strdup("invalid model number");
            return NULL;
        }
        want_model = norm;
    }
    for (i = 0; i < s->nvariants; ++i) {
        Variant *v = &s->variants[i];
        int pen;
        if (want_model && *want_model && !js_streq(v->model, want_model)) continue;
        if (features && *features && !js_csv_subset(v->features, features)) continue;
        if (params && *params && !js_csv_subset(v->params, params)) continue;
        if (program && *program && !js_variant_has_program(s, v, program)) continue;
        pen = features ? js_feature_penalty(v->features, features) : 0;
        if (pen < best_penalty || (pen == best_penalty && best && strcmp(v->name, best->name) < 0)) {
            best = v;
            best_penalty = pen;
        }
    }
    if (!best && !(want_model && *want_model) && !(features && *features) &&
        !(params && *params) && s->default_variant && *s->default_variant) {
        Variant *d = js_find_variant(s, s->default_variant);
        if (d && (!program || js_variant_has_program(s, d, program))) best = d;
    }
    if (!best && s->nvariants == 1 && !want_model && !(features && *features) && !(params && *params))
        best = &s->variants[0];
    if (!best) {
        if (why) *why = js_strdup("no variant matches the requested model/features/parameters");
        return NULL;
    }
    if (!allow_ambiguous && !variant_name && !model && !(features && *features) && !(params && *params)) {
        /* If several variants are present and no selector was supplied, do not
           silently pick one.  The caller may have inferred a model from stdin. */
        if (s->nvariants > 1) {
            if (why) *why = js_strdup("several variants are embedded; specify --model, --variant, --features, or --params");
            return NULL;
        }
    }
    return best;
}

static void js_print_variants(Suite *s, int json) {
    size_t i;
    if (json) {
        printf("{\n  \"suite_version\": \"%s\",\n  \"jorek_version\": \"%s\",\n  \"variants\": [\n", s->suite_version, s->jorek_version);
        for (i = 0; i < s->nvariants; ++i) {
            Variant *v = &s->variants[i];
            printf("    {\"name\": \"%s\", \"model\": \"%s\", \"features\": \"%s\", \"params\": \"%s\"}%s\n",
                   v->name, v->model, v->features, v->params, i + 1 == s->nvariants ? "" : ",");
        }
        printf("  ]\n}\n");
        return;
    }
    printf("JOREK variants embedded in this suite:\n");
    printf("  %-24s %-8s %-8s %s\n", "VARIANT", "MODEL", "GROUP", "FEATURES / PARAMETERS");
    for (i = 0; i < s->nvariants; ++i) {
        Variant *v = &s->variants[i];
        printf("  %-24s %-8s %-8d %s | %s\n", v->name, v->model, v->group, v->features, v->params);
    }
}

static void js_print_programs(Suite *s, Variant *v, int json) {
    size_t i;
    int first = 1;
    if (json) {
        printf("{\"variant\": \"%s\", \"programs\": [", v ? v->name : "");
        for (i = 0; i < s->nprograms; ++i) {
            Program *p = &s->programs[i];
            if (v && !js_streq(p->variant, v->name)) continue;
            printf("%s{\"name\": \"%s\", \"bytes\": %llu}", first ? "" : ", ",
                   p->name, (unsigned long long)p->size);
            first = 0;
        }
        printf("]}\n");
        return;
    }
    printf("Programs%s%s:\n", v ? " for variant " : "", v ? v->name : "");
    for (i = 0; i < s->nprograms; ++i) {
        Program *p = &s->programs[i];
        if (v && !js_streq(p->variant, v->name)) continue;
        printf("  %-40s %10llu bytes\n", p->name, (unsigned long long)p->size);
    }
}

static void js_print_info(Suite *s) {
    size_t i;
    uint64_t payload = 0;
    for (i = 0; i < s->nprograms; ++i) payload += s->programs[i].size;
    printf("JOREK single-file suite\n");
    printf("  suite version : %s\n", s->suite_version);
    printf("  JOREK version : %s\n", s->jorek_version);
    printf("  build id      : %s\n", s->build_id);
    printf("  variants      : %lu\n", (unsigned long)s->nvariants);
    printf("  programs      : %lu\n", (unsigned long)s->nprograms);
    printf("  groups        : %lu\n", (unsigned long)s->ngroups);
    printf("  payload       : %.2f MiB uncompressed\n", (double)payload / 1048576.0);
    printf("  file          : %s\n", s->path);
}

/* ------------------------------------------------------------------------- */
/* Self path, extraction, cache                                             */
/* ------------------------------------------------------------------------- */
static char *js_self_path(const char *argv0) {
#ifdef _WIN32
    wchar_t wbuf[PATH_MAX];
    DWORD n = GetModuleFileNameW(NULL, wbuf, (DWORD)(sizeof(wbuf) / sizeof(wbuf[0])));
    char *buf;
    int m;
    if (n == 0 || n >= sizeof(wbuf) / sizeof(wbuf[0])) return js_strdup(argv0);
    m = WideCharToMultiByte(CP_UTF8, 0, wbuf, -1, NULL, 0, NULL, NULL);
    if (m <= 0) return js_strdup(argv0);
    buf = (char *)js_malloc((size_t)m);
    WideCharToMultiByte(CP_UTF8, 0, wbuf, -1, buf, m, NULL, NULL);
    return buf;
#else
#  ifdef __APPLE__
    uint32_t size = 0;
    if (_NSGetExecutablePath(NULL, &size) == 0) {
        char *buf = (char *)js_malloc(size + 1);
        if (_NSGetExecutablePath(buf, &size) == 0) {
            char *real = realpath(buf, NULL);
            if (real) { free(buf); return real; }
            return buf;
        }
        free(buf);
    }
#  endif
    {
        char buf[PATH_MAX];
        ssize_t n = readlink("/proc/self/exe", buf, sizeof(buf) - 1);
        if (n > 0) { buf[n] = '\0'; return js_strdup(buf); }
    }
    {
        char *real = realpath(argv0, NULL);
        if (real) return real;
    }
    return js_strdup(argv0);
#endif
}

static char *js_cache_root(void) {
    const char *e = g_cache_override ? g_cache_override : getenv("JOREK_SUITE_CACHE");
    if (e && *e) return js_strdup(e);
#ifdef _WIN32
    e = getenv("LOCALAPPDATA");
    if (e && *e) return js_path_join(e, "jorek-suite\\cache");
    e = getenv("TEMP");
    if (e && *e) return js_path_join(e, "jorek-suite-cache");
    return js_strdup("jorek-suite-cache");
#else
#  ifdef __APPLE__
    e = getenv("HOME");
    if (e && *e) {
        char *base = js_path_join(e, "Library/Caches");
        char *p = js_path_join(base, "jorek-suite");
        free(base);
        return p;
    }
#  endif
    e = getenv("XDG_CACHE_HOME");
    if (e && *e) return js_path_join(e, "jorek-suite");
    e = getenv("HOME");
    if (e && *e) {
        char *base = js_path_join(e, ".cache");
        char *p = js_path_join(base, "jorek-suite");
        free(base);
        return p;
    }
    e = getenv("TMPDIR");
    if (e && *e) return js_path_join(e, "jorek-suite-cache");
    return js_strdup("/tmp/jorek-suite-cache");
#endif
}

static int js_sha_file(const char *path, uint8_t out[32]) {
    FILE *f = js_fopen(path, "rb");
    unsigned char buf[65536];
    size_t n;
    SHA256_CTX ctx;
    if (!f) return -1;
    sha256_init(&ctx);
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) sha256_update(&ctx, buf, n);
    if (ferror(f)) { fclose(f); return -1; }
    fclose(f);
    sha256_final(&ctx, out);
    return 0;
}

static int js_file_has_sha(const char *path, const uint8_t want[32]) {
    uint8_t got[32];
    if (js_sha_file(path, got) != 0) return 0;
    return memcmp(got, want, 32) == 0;
}

static int js_write_atomic(const char *path, const void *data, size_t len, unsigned mode) {
    char *tmp;
#ifdef _WIN32
    (void)mode;
#endif
    FILE *f;
    int ok = 0;
    size_t n = strlen(path) + 64;
    tmp = (char *)js_malloc(n);
#ifdef _WIN32
    snprintf(tmp, n, "%s.tmp%ld", path, (long)js_getpid());
#else
    snprintf(tmp, n, "%s.tmp%ld", path, (long)js_getpid());
#endif
    f = js_fopen(tmp, "wb");
    if (!f) { free(tmp); return -1; }
    if (len == 0 || fwrite(data, 1, len, f) == len) ok = 1;
    if (fflush(f) != 0) ok = 0;
#ifndef _WIN32
    if (ok && fsync(fileno(f)) != 0) ok = 0;
#endif
    if (fclose(f) != 0) ok = 0;
    if (!ok) { remove(tmp); free(tmp); return -1; }
#ifndef _WIN32
    if (chmod(tmp, (mode_t)mode) != 0) { remove(tmp); free(tmp); return -1; }
#endif
#ifdef _WIN32
    remove(path);
    if (rename(tmp, path) != 0) { remove(tmp); free(tmp); return -1; }
#else
    if (rename(tmp, path) != 0) {
        remove(tmp);
        free(tmp);
        return -1;
    }
#endif
    free(tmp);
    return 0;
}

static unsigned char *js_read_group(Suite *s, Group *g, size_t *out_len) {
    unsigned char *cbuf, *ubuf;
    unsigned long dlen, slen;
    if (g->csize > (uint64_t)SIZE_MAX || g->usize > (uint64_t)SIZE_MAX)
        js_die(4, "group %d is too large for this platform", g->id);
    if (g->csize == 0 || g->usize == 0)
        js_die(4, "group %d has invalid sizes", g->id);
    cbuf = (unsigned char *)js_malloc((size_t)g->csize);
    ubuf = (unsigned char *)js_malloc((size_t)g->usize);
    if (js_read_at(s, s->archive_off + g->off, cbuf, (size_t)g->csize) != 0)
        js_die(4, "cannot read group %d from suite", g->id);
    dlen = (unsigned long)g->usize;
    slen = (unsigned long)g->csize;
    if (puff(ubuf, &dlen, cbuf, &slen) != 0 || dlen != (unsigned long)g->usize)
        js_die(4, "cannot decompress group %d (corrupt suite)", g->id);
    free(cbuf);
    {
        uint8_t got[32];
        sha256_bytes(ubuf, (size_t)g->usize, got);
        if (memcmp(got, g->sha, 32) != 0)
            js_die(4, "group %d digest mismatch (corrupt suite)", g->id);
    }
    *out_len = (size_t)g->usize;
    return ubuf;
}

static int js_extract_program(Suite *s, Program *p, const char *dest, int verify_only) {
    Group *g = js_find_group(s, p->group);
    unsigned char *group;
    size_t glen = 0;
    char *dir;
    if (!g) return -1;
    if (p->off > g->usize || p->size > g->usize - p->off) {
        js_warn("program %s has invalid offsets", p->name);
        return -1;
    }
    dir = js_strdup(dest);
    {
        char *slash = strrchr(dir, '/');
#ifdef _WIN32
        char *bslash = strrchr(dir, '\\');
        if (!slash || (bslash && bslash > slash)) slash = bslash;
#endif
        if (slash) { *slash = '\0'; if (*dir) js_mkdir_p(dir); }
    }
    free(dir);
    if (js_file_has_sha(dest, p->sha)) {
        js_log("cached %s", dest);
        return 0;
    }
    group = js_read_group(s, g, &glen);
    if (verify_only) {
        uint8_t got[32];
        sha256_bytes(group + p->off, (size_t)p->size, got);
        free(group);
        return memcmp(got, p->sha, 32) == 0 ? 0 : -1;
    }
    {
        uint8_t got[32];
        sha256_bytes(group + p->off, (size_t)p->size, got);
        if (memcmp(got, p->sha, 32) != 0) {
            free(group);
            js_warn("embedded program %s failed digest verification", p->name);
            return -1;
        }
    }
    if (js_write_atomic(dest, group + p->off, (size_t)p->size,
                        p->mode ? p->mode : 0755) != 0) {
        free(group);
        return -1;
    }
    free(group);
    js_log("extracted %s", dest);
    return 0;
}

static char *js_materialize(Suite *s, Variant *v, Program *p, int ephemeral, int *is_temp) {
    char idbuf[65];
    char *root, *dir, *dest;
    *is_temp = 0;
    if (!ephemeral) {
        root = js_cache_root();
        hex32(s->manifest_sha, idbuf);
        {
            char *a = js_path_join(root, idbuf);
            free(root);
            root = a;
        }
        dir = js_path_join(root, v->name);
        free(root);
        dest = js_path_join(dir, p->name);
        free(dir);
        {
            char *parent = js_strdup(dest);
            char *slash = strrchr(parent, '/');
#ifdef _WIN32
            char *bslash = strrchr(parent, '\\');
            if (!slash || (bslash && bslash > slash)) slash = bslash;
#endif
            if (slash) *slash = '\0';
            if (*parent) js_mkdir_p(parent);
            free(parent);
        }
        if (js_file_has_sha(dest, p->sha)) return dest;
        if (js_extract_program(s, p, dest, 0) == 0) return dest;
        js_log("cache extraction failed for %s: %s", dest, strerror(errno));
        free(dest);
        /* Fall through to a temporary file if the cache is not usable. */
    }
    {
#ifdef _WIN32
        char tmpdir[PATH_MAX];
        DWORD n = GetTempPathA((DWORD)sizeof(tmpdir), tmpdir);
        if (n == 0 || n >= sizeof(tmpdir)) js_strlcpy(tmpdir, ".", sizeof(tmpdir));
        {
            char name[128];
            snprintf(name, sizeof(name), "jorek-suite-%ld-%s", (long)js_getpid(), p->name);
            dest = js_path_join(tmpdir, name);
        }
#else
        const char *td = getenv("TMPDIR");
        char *tmpl;
        if (!td || !*td) td = "/tmp";
        tmpl = js_path_join(td, "jorek-suite-XXXXXX");
        {
            int fd = mkstemp(tmpl);
            if (fd < 0) js_die(4, "cannot create a temporary file: %s", strerror(errno));
            close(fd);
            unlink(tmpl);
        }
        dest = tmpl;
#endif
        if (js_extract_program(s, p, dest, 0) != 0)
            js_die(4, "cannot extract program %s", p->name);
        *is_temp = 1;
        return dest;
    }
}

/* ------------------------------------------------------------------------- */
/* Child process                                                            */
/* ------------------------------------------------------------------------- */
#ifndef _WIN32
static int js_run_posix(const char *path, char **args, const void *stdin_data,
                        size_t stdin_len, int wait_child) {
    pid_t pid;
    int status = 0;
    pid = fork();
    if (pid < 0) js_die(5, "fork failed: %s", strerror(errno));
    if (pid == 0) {
        execv(path, args);
        fprintf(stderr, "jorek-suite: cannot execute %s: %s\n", path, strerror(errno));
        _exit(127);
    }
    (void)stdin_data;
    (void)stdin_len;
    if (wait_child) {
        if (waitpid(pid, &status, 0) < 0) js_die(5, "waitpid failed: %s", strerror(errno));
        if (WIFEXITED(status)) return WEXITSTATUS(status);
        if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
        return 1;
    }
    /* Without wait we cannot clean temporary files; the caller only uses this
       path for cache materialisation. */
    return 0;
}

static int js_run_pipe(const char *path, char **args, const void *stdin_data, size_t stdin_len) {
    int fds[2];
    pid_t pid;
    int status = 0;
    size_t off = 0;
    if (pipe(fds) != 0) js_die(5, "pipe failed: %s", strerror(errno));
    pid = fork();
    if (pid < 0) js_die(5, "fork failed: %s", strerror(errno));
    if (pid == 0) {
        close(fds[1]);
        if (dup2(fds[0], STDIN_FILENO) < 0) _exit(126);
        close(fds[0]);
        execv(path, args);
        _exit(127);
    }
    close(fds[0]);
    while (off < stdin_len) {
        ssize_t n = write(fds[1], (const char *)stdin_data + off, stdin_len - off);
        if (n < 0) {
            if (errno == EINTR) continue;
            break;
        }
        off += (size_t)n;
    }
    close(fds[1]);
    if (waitpid(pid, &status, 0) < 0) js_die(5, "waitpid failed: %s", strerror(errno));
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return 1;
}
#else
static void js_quote_arg(char *out, size_t outsz, const char *arg) {
    size_t n = 0;
    const char *p;
    int need = strchr(arg, ' ') || strchr(arg, '\t') || strchr(arg, '"');
    if (need && n + 1 < outsz) out[n++] = '"';
    for (p = arg; *p && n + 2 < outsz; ++p) {
        if (*p == '"' && n + 2 < outsz) out[n++] = '\\';
        out[n++] = *p;
    }
    if (need && n + 1 < outsz) out[n++] = '"';
    out[n] = '\0';
}

static int js_run_windows(const char *path, char **args, const void *stdin_data,
                          size_t stdin_len, int wait_child) {
    wchar_t *wpath = js_utf8_to_wide(path);
    char *cmd;
    size_t cap = 65536, used = 0;
    int i;
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    char *tmpname = NULL;
    HANDLE hstdin = NULL;
    DWORD code = 1;
    cmd = (char *)js_malloc(cap);
    cmd[0] = '\0';
    js_quote_arg(cmd, cap, path);
    used = strlen(cmd);
    for (i = 1; args[i]; ++i) {
        char q[32768];
        js_quote_arg(q, sizeof(q), args[i]);
        if (used + strlen(q) + 2 >= cap) break;
        cmd[used++] = ' ';
        strcpy(cmd + used, q);
        used += strlen(q);
    }
    if (stdin_data) {
        char tmpdir[PATH_MAX];
        DWORD n = GetTempPathA((DWORD)sizeof(tmpdir), tmpdir);
        if (n == 0 || n >= sizeof(tmpdir)) js_strlcpy(tmpdir, ".", sizeof(tmpdir));
        tmpname = (char *)js_malloc(strlen(tmpdir) + 64);
        sprintf(tmpname, "%sjorek-suite-stdin-%ld", tmpdir, (long)js_getpid());
        {
            FILE *f = js_fopen(tmpname, "wb");
            if (!f) js_die(5, "cannot create stdin file");
            if (stdin_len) fwrite(stdin_data, 1, stdin_len, f);
            fclose(f);
        }
        hstdin = CreateFileA(tmpname, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    }
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    if (hstdin != INVALID_HANDLE_VALUE && hstdin) {
        si.dwFlags |= STARTF_USESTDHANDLES;
        si.hStdInput = hstdin;
        si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
        si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    }
    if (!wpath || !CreateProcessW(wpath, js_utf8_to_wide(cmd), NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
        js_die(5, "cannot execute %s (error %lu)", path, (unsigned long)GetLastError());
    }
    if (hstdin && hstdin != INVALID_HANDLE_VALUE) CloseHandle(hstdin);
    if (!wait_child) {
        CloseHandle(pi.hProcess);
        CloseHandle(pi.hThread);
        return 0;
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    if (tmpname) { DeleteFileA(tmpname); free(tmpname); }
    free(cmd);
    free(wpath);
    (void)stdin_len;
    return (int)code;
}
#endif

/* ------------------------------------------------------------------------- */
/* Help                                                                     */
/* ------------------------------------------------------------------------- */
static const char *js_progname(const char *argv0) {
    const char *p = argv0;
    const char *s;
    if (!argv0) return "jorek-suite";
    for (s = argv0; *s; ++s) if (*s == '/' || *s == '\\') p = s + 1;
    return p;
}

static void js_usage(const char *prog) {
    printf(
"JOREK single-file launcher %s\n"
"\n"
"USAGE\n"
"  %s [OPTIONS] [--] [JOREK-ARGS...]\n"
"  %s --model 303 < namelist\n"
"  mpirun -np 8 %s --model 303 < namelist\n"
"\n"
"SELECTION\n"
"  --variant NAME          Select an embedded variant by exact name.\n"
"  --model N               Select the variant for physics model N (e.g. 303 or model303).\n"
"  --features LIST         Require comma-separated features (hdf5,fftw,mumps,...).\n"
"  --params LIST           Require comma-separated hard-coded parameters\n"
"                          (e.g. n_tor=3,n_plane=4,n_order=3).\n"
"  --program NAME          Select an embedded executable (default: jorek2_main).\n"
"  --auto                  Infer the model from a leading '@jorek_model: N' line\n"
"                          on standard input (enabled automatically when ambiguous).\n"
"  --no-auto               Disable standard-input model inference.\n"
"\n"
"INSPECTION\n"
"  --list, -l              List embedded variants.\n"
"  --list-programs         List programs (for the selected variant, if any).\n"
"  --list-features         List all feature names present in the manifest.\n"
"  --info                  Show suite metadata and payload sizes.\n"
"  --json                  Emit machine-readable output for --list/--info.\n"
"  --version               Print the launcher version.\n"
"  --help, -h              Show this help.\n"
"\n"
"EXTRACTION\n"
"  --extract DIR           Extract all selected programs into DIR (no execution).\n"
"  --extract-program DIR   Extract only the selected program into DIR.\n"
"  --extract-only          Print the path of the extracted program and do not run it.\n"
"  --cache DIR             Override the extraction cache directory.\n"
"  --no-cache              Never use the persistent cache.\n"
"  --ephemeral             Use an unlinked temporary file and delete it after exit.\n"
"  --print-command         Print the resolved executable path and exit.\n"
"\n"
"EXECUTION\n"
"  --wait                  Wait for the child and forward its exit status.\n"
"  --quiet                 Suppress informational messages.\n"
"  -v, --verbose           Print extraction and selection details.\n"
"  --                      Stop option parsing; pass the rest to JOREK.\n"
"\n"
"ENVIRONMENT\n"
"  JOREK_SUITE_MODEL, JOREK_SUITE_VARIANT, JOREK_SUITE_PROGRAM,\n"
"  JOREK_SUITE_FEATURES, JOREK_SUITE_PARAMS, JOREK_SUITE_CACHE.\n"
"\n"
"EXIT STATUS\n"
"  0-127  exit status of the selected program\n"
"  2      command-line usage error\n"
"  3      malformed or corrupted suite\n"
"  4      extraction or verification failure\n"
"  5      could not start the child process\n"
"\n"
"EXAMPLES\n"
"  %s --list\n"
"  %s --model 303 --program jorek2vtk -- -i input.nml\n"
"  %s --features hdf5,fftw --params n_tor=3,n_plane=4 --print-command\n"
"  jorek2_main < input.nml          (busybox-style name dispatch is supported)\n",
        JS_VERSION, prog, prog, prog, prog, prog, prog);
}

/* ------------------------------------------------------------------------- */
/* Model inference from a namelist                                          */
/* ------------------------------------------------------------------------- */
static int js_infer_model(const char *buf, size_t len, char *out, size_t outsz) {
    const char *p = buf;
    const char *end = buf + len;
    while (p < end) {
        const char *line = p;
        const char *eol = memchr(p, '\n', (size_t)(end - p));
        size_t n = eol ? (size_t)(eol - p) : (size_t)(end - p);
        char small[256];
        size_t i;
        if (n >= sizeof(small)) n = sizeof(small) - 1;
        memcpy(small, line, n);
        small[n] = '\0';
        for (i = 0; i < n; ++i) small[i] = (char)tolower((unsigned char)small[i]);
        if (strstr(small, "jorek_model") || strstr(small, "model =") || strstr(small, "model=")) {
            const char *q = small;
            while (*q && !isdigit((unsigned char)*q)) q++;
            if (*q) {
                const char *r = q;
                while (isdigit((unsigned char)*r)) r++;
                if ((size_t)(r - q) < outsz) {
                    memcpy(out, q, (size_t)(r - q));
                    out[r - q] = '\0';
                    return 0;
                }
            }
        }
        p = eol ? eol + 1 : end;
    }
    return -1;
}

static char *js_read_stdin(size_t *len) {
    size_t cap = 8192, n = 0;
    char *buf = (char *)js_malloc(cap);
    for (;;) {
        size_t got;
        if (n + 4096 > cap) {
            if (cap >= JS_MAX_INPUT) break;
            cap *= 2;
            buf = (char *)js_realloc(buf, cap);
        }
        got = fread(buf + n, 1, 4096, stdin);
        n += got;
        if (got == 0) break;
    }
    *len = n;
    return buf;
}

/* ------------------------------------------------------------------------- */
/* Main                                                                     */
/* ------------------------------------------------------------------------- */
int main(int argc, char **argv) {
    Suite suite;
    const char *self = NULL;
    const char *opt_variant = NULL, *opt_model = NULL, *opt_features = NULL;
    const char *opt_params = NULL, *opt_program = NULL;
    const char *opt_extract = NULL, *opt_extract_one = NULL, *opt_cache = NULL;
    int opt_list = 0, opt_list_programs = 0, opt_list_features = 0, opt_info = 0;
    int opt_json = 0, opt_extract_only = 0, opt_no_cache = 0, opt_ephemeral = 0;
    int opt_wait = 0, opt_print_command = 0, opt_auto = -1, opt_help = 0;
    int i, arg_start = argc;
    char **child_args;
    char stdin_model[64];
    void *stdin_data = NULL;
    size_t stdin_len = 0;
    Variant *variant = NULL;
    Program *program = NULL;
    char *exe = NULL;
    int is_temp = 0;
    char *why = NULL;

    setvbuf(stdout, NULL, _IOLBF, 0);
    /* Busybox-style dispatch: jorek2_main, jorek_model303, model303. */
    {
        const char *base = js_progname(argv[0]);
        if (strncmp(base, "jorek_model", 11) == 0 && isdigit((unsigned char)base[11])) opt_model = base + 11;
        else if (strncmp(base, "jorek2_", 7) == 0) opt_program = base;
        else if (strncmp(base, "model", 5) == 0 && isdigit((unsigned char)base[5])) opt_model = base + 5;
    }
    if (getenv("JOREK_SUITE_MODEL")) opt_model = getenv("JOREK_SUITE_MODEL");
    if (getenv("JOREK_SUITE_VARIANT")) opt_variant = getenv("JOREK_SUITE_VARIANT");
    if (getenv("JOREK_SUITE_PROGRAM")) opt_program = getenv("JOREK_SUITE_PROGRAM");
    if (getenv("JOREK_SUITE_FEATURES")) opt_features = getenv("JOREK_SUITE_FEATURES");
    if (getenv("JOREK_SUITE_PARAMS")) opt_params = getenv("JOREK_SUITE_PARAMS");
    opt_cache = getenv("JOREK_SUITE_CACHE");

    for (i = 1; i < argc; ++i) {
        const char *a = argv[i];
        if (js_streq(a, "--")) { arg_start = i + 1; break; }
        if (a[0] != '-') { arg_start = i; break; }
        if (js_streq(a, "--help") || js_streq(a, "-h")) opt_help = 1;
        else if (js_streq(a, "--version")) { printf("jorek-suite %s (format %u)\n", JS_VERSION, JS_FORMAT_VERSION); return 0; }
        else if (js_streq(a, "--list") || js_streq(a, "-l")) opt_list = 1;
        else if (js_streq(a, "--list-programs")) opt_list_programs = 1;
        else if (js_streq(a, "--list-features")) opt_list_features = 1;
        else if (js_streq(a, "--info")) opt_info = 1;
        else if (js_streq(a, "--json")) opt_json = 1;
        else if (js_streq(a, "--extract-only")) opt_extract_only = 1;
        else if (js_streq(a, "--no-cache")) opt_no_cache = 1;
        else if (js_streq(a, "--ephemeral")) opt_ephemeral = 1;
        else if (js_streq(a, "--wait")) opt_wait = 1;
        else if (js_streq(a, "--print-command")) opt_print_command = 1;
        else if (js_streq(a, "--auto")) opt_auto = 1;
        else if (js_streq(a, "--no-auto")) opt_auto = 0;
        else if (js_streq(a, "--quiet")) g_quiet = 1;
        else if (js_streq(a, "--verbose") || js_streq(a, "-v")) g_verbose = 1;
        else if (js_streq(a, "--variant")) { if (++i >= argc) js_die(2, "--variant requires an argument"); opt_variant = argv[i]; }
        else if (js_streq(a, "--model")) { if (++i >= argc) js_die(2, "--model requires an argument"); opt_model = argv[i]; }
        else if (js_streq(a, "--features")) { if (++i >= argc) js_die(2, "--features requires an argument"); opt_features = argv[i]; }
        else if (js_streq(a, "--params")) { if (++i >= argc) js_die(2, "--params requires an argument"); opt_params = argv[i]; }
        else if (js_streq(a, "--program")) { if (++i >= argc) js_die(2, "--program requires an argument"); opt_program = argv[i]; }
        else if (js_streq(a, "--extract")) { if (++i >= argc) js_die(2, "--extract requires a directory"); opt_extract = argv[i]; }
        else if (js_streq(a, "--extract-program")) { if (++i >= argc) js_die(2, "--extract-program requires a directory"); opt_extract_one = argv[i]; }
        else if (js_streq(a, "--cache")) { if (++i >= argc) js_die(2, "--cache requires a directory"); opt_cache = argv[i]; }
        else if (js_streq(a, "--archive")) { if (++i >= argc) js_die(2, "--archive requires a file"); self = argv[i]; }
        else js_die(2, "unknown option '%s' (use --help)", a);
    }
    if (arg_start == argc) arg_start = argc;
    if (opt_help) { js_usage(js_progname(argv[0])); return 0; }
    g_cache_override = opt_cache;
    if (!self) self = js_self_path(argv[0]);
    js_load_suite(self, &suite);

    if (opt_list) { js_print_variants(&suite, opt_json); return 0; }
    if (opt_info) { js_print_info(&suite); return 0; }
    if (opt_list_features) {
        /* Union of features, printed one per line. */
        size_t vi;
        for (vi = 0; vi < suite.nvariants; ++vi) {
            const char *p = suite.variants[vi].features;
            while (p && *p) {
                const char *c = strchr(p, ',');
                size_t n = c ? (size_t)(c - p) : strlen(p);
                if (n) printf("%.*s\n", (int)n, p);
                if (!c) break;
                p = c + 1;
            }
        }
        return 0;
    }
    if (opt_list_programs) {
        Variant *v = NULL;
        if (opt_variant || opt_model || opt_features || opt_params) {
            v = js_select_variant(&suite, opt_variant, opt_model, opt_features, opt_params, NULL, 1, &why);
            if (!v) js_die(2, "%s", why ? why : "no matching variant");
        }
        js_print_programs(&suite, v, opt_json);
        return 0;
    }

    /* Infer the model from stdin only when no explicit selector exists. */
    if (!opt_variant && !opt_model && opt_auto != 0) {
        if (opt_auto == 1 || (!isatty(0) && suite.nvariants > 1)) {
            stdin_data = js_read_stdin(&stdin_len);
            if (js_infer_model((const char *)stdin_data, stdin_len, stdin_model, sizeof(stdin_model)) == 0) {
                opt_model = stdin_model;
                js_log("inferred model %s from standard input", stdin_model);
            } else if (opt_auto == 1 && suite.nvariants > 1) {
                js_warn("could not infer a model from standard input");
            }
        }
    }

    variant = js_select_variant(&suite, opt_variant, opt_model, opt_features, opt_params,
                                opt_program, 0, &why);
    if (!variant) {
        fprintf(stderr, "jorek-suite: %s\n\n", why ? why : "no matching variant");
        js_print_variants(&suite, 0);
        fprintf(stderr, "\nUse --model N, --variant NAME, --list, or --help.\n");
        return 2;
    }
    if (!opt_program || !*opt_program) opt_program = "jorek2_main";
    program = js_find_program(&suite, variant, opt_program);
    if (!program) {
        /* A single executable per variant is a common case; use it. */
        program = js_default_program(&suite, variant);
        if (!program) js_die(2, "variant '%s' contains no programs", variant->name);
        if (opt_program && strcmp(opt_program, "jorek2_main") != 0)
            js_die(2, "variant '%s' has no program '%s'", variant->name, opt_program);
    }
    if (opt_extract || opt_extract_one) {
        const char *dir = opt_extract ? opt_extract : opt_extract_one;
        char *dest;
        if (js_mkdir_p(dir) != 0) js_die(4, "cannot create extraction directory '%s'", dir);
        if (opt_extract) {
            size_t k;
            for (k = 0; k < suite.nprograms; ++k) {
                Program *p = &suite.programs[k];
                char *d;
                if (!js_streq(p->variant, variant->name)) continue;
                d = js_path_join(dir, p->name);
                if (js_extract_program(&suite, p, d, 0) != 0) js_die(4, "cannot extract %s", p->name);
                printf("%s\n", d);
                free(d);
            }
            return 0;
        }
        dest = js_path_join(dir, program->name);
        if (js_extract_program(&suite, program, dest, 0) != 0) js_die(4, "cannot extract %s", program->name);
        printf("%s\n", dest);
        free(dest);
        return 0;
    }

    if (opt_no_cache) opt_ephemeral = 1;
    exe = js_materialize(&suite, variant, program, opt_ephemeral, &is_temp);
    if (opt_extract_only) { printf("%s\n", exe); if (is_temp) { /* keep for caller */ } return 0; }
    if (opt_print_command) { printf("%s\n", exe); if (is_temp) { /* keep for caller */ } return 0; }

    {
        int nargs = argc - arg_start;
        child_args = (char **)js_calloc((size_t)nargs + 2, sizeof(char *));
        child_args[0] = (char *)program->name;
        for (i = 0; i < nargs; ++i) child_args[i + 1] = argv[arg_start + i];
        child_args[nargs + 1] = NULL;
    }
    js_setenv("JOREK_SUITE_VARIANT", variant->name, 1);
    js_setenv("JOREK_SUITE_MODEL", variant->model, 1);
    js_setenv("JOREK_SUITE_PROGRAM", program->name, 1);
    if (opt_features && *opt_features) js_setenv("JOREK_SUITE_FEATURES", opt_features, 1);
    if (opt_params && *opt_params) js_setenv("JOREK_SUITE_PARAMS", opt_params, 1);

#ifdef _WIN32
    (void)opt_wait;
    {
        int rc = js_run_windows(exe, child_args, stdin_data, stdin_len, 1);
        if (is_temp) remove(exe);
        free(exe);
        return rc;
    }
#else
    if (is_temp || stdin_data || opt_wait) {
        int rc;
        if (stdin_data) rc = js_run_pipe(exe, child_args, stdin_data, stdin_len);
        else rc = js_run_posix(exe, child_args, NULL, 0, 1);
        if (is_temp) unlink(exe);
        free(exe);
        return rc;
    }
    execv(exe, child_args);
    js_die(5, "cannot execute %s: %s", exe, strerror(errno));
#endif
    return 0;
}
