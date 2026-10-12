#include "ictrl.h"
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void buf_init(buf *b) { b->data = NULL; b->len = 0; b->cap = 0; }

void buf_free(buf *b) {
    free(b->data);
    b->data = NULL;
    b->len = b->cap = 0;
}

int buf_append(buf *b, const void *data, size_t n) {
    if (b->len + n + 1 > b->cap) {
        size_t cap = b->cap ? b->cap : 256;
        while (cap < b->len + n + 1) cap *= 2;
        uint8_t *p = realloc(b->data, cap);
        if (!p) return -1;
        b->data = p;
        b->cap = cap;
    }
    memcpy(b->data + b->len, data, n);
    b->len += n;
    b->data[b->len] = 0;   // keep it printable as a C string
    return 0;
}

int buf_appendf(buf *b, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    char stack[512];
    int n = vsnprintf(stack, sizeof stack, fmt, ap);
    va_end(ap);
    if (n < 0) return -1;
    if ((size_t)n < sizeof stack) return buf_append(b, stack, (size_t)n);

    char *heap = malloc((size_t)n + 1);
    if (!heap) return -1;
    va_start(ap, fmt);
    vsnprintf(heap, (size_t)n + 1, fmt, ap);
    va_end(ap);
    int rc = buf_append(b, heap, (size_t)n);
    free(heap);
    return rc;
}

static const char HEX[] = "0123456789abcdef";

void hex_encode(const uint8_t *in, size_t n, char *out) {
    for (size_t i = 0; i < n; i++) {
        out[i * 2] = HEX[in[i] >> 4];
        out[i * 2 + 1] = HEX[in[i] & 15];
    }
    out[n * 2] = 0;
}

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

int hex_decode(const char *in, uint8_t *out, size_t outlen) {
    for (size_t i = 0; i < outlen; i++) {
        int hi = hexval(in[i * 2]), lo = hexval(in[i * 2 + 1]);
        if (hi < 0 || lo < 0) return -1;
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    return 0;
}

static const char B64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

size_t base64_encode(const uint8_t *in, size_t n, char *out) {
    size_t o = 0;
    for (size_t i = 0; i < n; i += 3) {
        uint32_t v = (uint32_t)in[i] << 16;
        if (i + 1 < n) v |= (uint32_t)in[i + 1] << 8;
        if (i + 2 < n) v |= in[i + 2];
        out[o++] = B64[(v >> 18) & 63];
        out[o++] = B64[(v >> 12) & 63];
        out[o++] = (i + 1 < n) ? B64[(v >> 6) & 63] : '=';
        out[o++] = (i + 2 < n) ? B64[v & 63] : '=';
    }
    out[o] = 0;
    return o;
}

size_t base64_decode(const char *in, size_t n, uint8_t *out) {
    static int8_t rev[256];
    static int built = 0;
    if (!built) {
        memset(rev, -1, sizeof rev);
        for (int i = 0; i < 64; i++) rev[(uint8_t)B64[i]] = (int8_t)i;
        built = 1;
    }
    size_t o = 0;
    uint32_t acc = 0;
    int bits = 0;
    for (size_t i = 0; i < n; i++) {
        int8_t v = rev[(uint8_t)in[i]];
        if (v < 0) continue;   // skip '=', newlines, whitespace
        acc = (acc << 6) | (uint32_t)v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out[o++] = (uint8_t)(acc >> bits);
        }
    }
    return o;
}

// Bounded JSON walk: a nested "error" or that word in a UI label is not an
// RPC failure. The decrypted payload is length-delimited, not NUL-terminated.
typedef struct { const uint8_t *p, *end; } json_cursor;

static void json_space(json_cursor *c) {
    while (c->p < c->end && strchr(" \t\r\n", *c->p) && *c->p) c->p++;
}

static int json_string(json_cursor *c, char *key, size_t cap) {
    if (c->p == c->end || *c->p++ != '"') return 0;
    size_t n = 0;
    while (c->p < c->end) {
        unsigned ch = *c->p++;
        if (ch == '"') { if (cap) key[n < cap ? n : cap - 1] = 0; return 1; }
        if (ch < 0x20) return 0;
        if (ch == '\\') {
            if (c->p == c->end) return 0;
            ch = *c->p++;
            if (ch == 'u') {
                ch = 0;
                for (int i = 0; i < 4; i++) {
                    if (c->p == c->end) return 0;
                    int hex = hexval((char)*c->p++);
                    if (hex < 0) return 0;
                    ch = (ch << 4) | (unsigned)hex;
                }
                // Non-ASCII keys cannot match the protocol's ASCII names.
                if (!ch || ch > 127) ch = '?';
            } else {
                if (!strchr("\"\\/bfnrt", ch) || !ch) return 0;
                switch (ch) {
                    case 'b': ch = '\b'; break; case 'f': ch = '\f'; break;
                    case 'n': ch = '\n'; break; case 'r': ch = '\r'; break;
                    case 't': ch = '\t'; break;
                }
            }
        }
        if (cap && n < cap - 1) key[n] = (char)ch;
        n++;
    }
    return 0;
}

static int json_value(json_cursor *c, int depth) {
    json_space(c);
    if (c->p == c->end || depth > 128) return 0;
    uint8_t ch = *c->p;
    if (ch == '"') return json_string(c, NULL, 0);
    if (ch == '{' || ch == '[') {
        uint8_t close = ch == '{' ? '}' : ']';
        c->p++; json_space(c);
        if (c->p < c->end && *c->p == close) { c->p++; return 1; }
        for (;;) {
            if (ch == '{') {
                if (!json_string(c, NULL, 0)) return 0;
                json_space(c);
                if (c->p == c->end || *c->p++ != ':') return 0;
            }
            if (!json_value(c, depth + 1)) return 0;
            json_space(c);
            if (c->p == c->end) return 0;
            if (*c->p == close) { c->p++; return 1; }
            if (*c->p++ != ',') return 0;
            json_space(c);
        }
    }
    const char *literal = ch == 't' ? "true" : ch == 'f' ? "false" : ch == 'n' ? "null" : NULL;
    if (literal) {
        size_t n = strlen(literal);
        if ((size_t)(c->end - c->p) < n || memcmp(c->p, literal, n)) return 0;
        c->p += n; return 1;
    }
    if (ch == '-') c->p++;
    if (c->p == c->end) return 0;
    if (*c->p == '0') c->p++;
    else {
        if (*c->p < '1' || *c->p > '9') return 0;
        do { c->p++; } while (c->p < c->end && *c->p >= '0' && *c->p <= '9');
    }
    if (c->p < c->end && *c->p == '.') {
        c->p++;
        if (c->p == c->end || *c->p < '0' || *c->p > '9') return 0;
        do { c->p++; } while (c->p < c->end && *c->p >= '0' && *c->p <= '9');
    }
    if (c->p < c->end && (*c->p == 'e' || *c->p == 'E')) {
        c->p++;
        if (c->p < c->end && (*c->p == '+' || *c->p == '-')) c->p++;
        if (c->p == c->end || *c->p < '0' || *c->p > '9') return 0;
        do { c->p++; } while (c->p < c->end && *c->p >= '0' && *c->p <= '9');
    }
    return 1;
}

int json_rpc_exit_status(const uint8_t *data, size_t len) {
    if (!data || !len) return 1;
    json_cursor c = {data, data + len};
    json_space(&c);
    if (c.p == c.end || *c.p++ != '{') return 1;
    unsigned results = 0, errors = 0;
    for (;;) {
        json_space(&c);
        char key[16];
        if (!json_string(&c, key, sizeof key)) return 1;
        json_space(&c);
        if (c.p == c.end || *c.p++ != ':') return 1;
        if (!strcmp(key, "result")) results++;
        if (!strcmp(key, "error")) errors++;
        if (!json_value(&c, 0)) return 1;
        json_space(&c);
        if (c.p == c.end) return 1;
        if (*c.p == '}') { c.p++; break; }
        if (*c.p++ != ',') return 1;
    }
    json_space(&c);
    return c.p == c.end && results == 1 && errors == 0 ? 0 : 1;
}
