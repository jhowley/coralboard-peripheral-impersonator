/* Coralboard bench — command parser. SDK-free, host-testable. See protocol.h. */

#include "protocol.h"

#include <string.h>

static int is_space(char c) { return c == ' ' || c == '\t' || c == '\r'; }

static int is_hex_digit(char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
}

int cb_hex_to_bytes(const char *hex, size_t hex_len, uint8_t *out, size_t out_len) {
    if (hex_len != out_len * 2) return -1;
    for (size_t i = 0; i < out_len; i++) {
        char hi = hex[i * 2], lo = hex[i * 2 + 1];
        if (!is_hex_digit(hi) || !is_hex_digit(lo)) return -1;
        uint8_t h = (uint8_t)(hi <= '9' ? hi - '0' : hi - 'a' + 10);
        uint8_t l = (uint8_t)(lo <= '9' ? lo - '0' : lo - 'a' + 10);
        out[i] = (uint8_t)((h << 4) | l);
    }
    return 0;
}

uint32_t cb_crc32_update(uint32_t crc, const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++)
            crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(crc & 1u)));
    }
    return crc;
}

uint32_t cb_crc32(const uint8_t *data, size_t len) {
    return ~cb_crc32_update(0xFFFFFFFFu, data, len);
}

int cb_wmask_allows(const uint8_t *wmask, uint16_t reg) {
    if (reg >= CB_REG_SPACE) return 0;
    return (wmask[reg >> 3] >> (reg & 7)) & 1;
}

const char *cb_err_name(cb_err err) {
    switch (err) {
    case CB_OK: return "OK";
    case CB_ERR_BAD_COMMAND: return "BAD_COMMAND";
    case CB_ERR_BAD_VALUE: return "BAD_VALUE";
    case CB_ERR_BAD_IMAGE: return "BAD_IMAGE";
    case CB_ERR_CRC: return "CRC";
    case CB_ERR_ARM_ACTIVE: return "ARM_ACTIVE";
    case CB_ERR_NOT_LOADED: return "NOT_LOADED";
    case CB_ERR_LEASE_TOO_LONG: return "LEASE_TOO_LONG";
    case CB_ERR_TRACE_PENDING: return "TRACE_PENDING";
    case CB_ERR_TIMER: return "TIMER";
    }
    return "UNKNOWN";
}

/* Parse an unsigned decimal with a hard digit cap; rejects signs, empty, junk. */
static int parse_u32(const char *s, size_t len, uint32_t *out) {
    if (len == 0 || len > 9) return -1;
    uint32_t value = 0;
    for (size_t i = 0; i < len; i++) {
        if (s[i] < '0' || s[i] > '9') return -1;
        value = value * 10u + (uint32_t)(s[i] - '0');
    }
    *out = value;
    return 0;
}

static int parse_hex_u32(const char *s, size_t len, uint32_t *out) {
    if (len == 0 || len > 8) return -1;
    uint32_t value = 0;
    for (size_t i = 0; i < len; i++) {
        char c = s[i];
        if (!is_hex_digit(c)) return -1;
        value = (value << 4) | (uint32_t)(c <= '9' ? c - '0' : c - 'a' + 10);
    }
    *out = value;
    return 0;
}

typedef struct {
    const char *key;
    const char *val;
    size_t key_len;
    size_t val_len;
} kv_token;

/* Split "key=value" tokens; returns count, or -1 when a token has no '='. */
static int split_kv(const char *s, size_t len, kv_token *tokens, int max_tokens) {
    int count = 0;
    size_t i = 0;
    while (i < len) {
        while (i < len && is_space(s[i])) i++;
        if (i >= len) break;
        size_t start = i;
        while (i < len && !is_space(s[i])) i++;
        size_t token_len = i - start;
        if (count >= max_tokens) return -1;
        const char *eq = memchr(s + start, '=', token_len);
        if (eq == NULL) return -1;
        size_t key_len = (size_t)(eq - (s + start));
        if (key_len == 0) return -1;
        tokens[count].key = s + start;
        tokens[count].key_len = key_len;
        tokens[count].val = eq + 1;
        tokens[count].val_len = token_len - key_len - 1;
        count++;
    }
    return count;
}

static int key_is(const kv_token *t, const char *name) {
    size_t n = strlen(name);
    return t->key_len == n && memcmp(t->key, name, n) == 0;
}

static int tag_is_valid(const char *s, size_t len) {
    if (len == 0 || len > CB_TAG_MAX) return 0;
    for (size_t i = 0; i < len; i++) {
        char c = s[i];
        int ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '.' ||
                 c == '_' || c == '-';
        if (!ok) return 0;
    }
    return 1;
}

static cb_err parse_load(const char *s, size_t len, cb_cmd *out) {
    kv_token tokens[9];
    int n = split_kv(s, len, tokens, 9);
    if (n < 0) return CB_ERR_BAD_COMMAND;

    const kv_token *crc = NULL, *flags = NULL, *tag = NULL, *image = NULL;
    const kv_token *wrap = NULL, *wmask = NULL, *init = NULL;
    for (int i = 0; i < n; i++) {
        const kv_token **slot = NULL;
        if (key_is(&tokens[i], "crc")) slot = &crc;
        else if (key_is(&tokens[i], "flags")) slot = &flags;
        else if (key_is(&tokens[i], "wrap")) slot = &wrap;
        else if (key_is(&tokens[i], "init")) slot = &init;
        else if (key_is(&tokens[i], "wmask")) slot = &wmask;
        else if (key_is(&tokens[i], "tag")) slot = &tag;
        else if (key_is(&tokens[i], "image")) slot = &image;
        else return CB_ERR_BAD_COMMAND;            /* unknown key: refuse, don't guess */
        if (*slot != NULL) return CB_ERR_BAD_COMMAND; /* duplicate key */
        *slot = &tokens[i];
    }
    if (!crc || !flags || !wrap || !init || !wmask || !tag || !image)
        return CB_ERR_BAD_COMMAND;

    uint32_t want_crc = 0, want_flags = 0;
    if (parse_hex_u32(crc->val, crc->val_len, &want_crc) != 0) return CB_ERR_BAD_VALUE;
    if (parse_hex_u32(flags->val, flags->val_len, &want_flags) != 0) return CB_ERR_BAD_VALUE;
    if (want_flags & ~(uint32_t)CB_LOAD_FLAG_AUTO_INC) return CB_ERR_BAD_VALUE;
    uint32_t want_wrap = 0;
    if (parse_u32(wrap->val, wrap->val_len, &want_wrap) != 0) return CB_ERR_BAD_VALUE;
    if (want_wrap < 1 || want_wrap > CB_REG_SPACE) return CB_ERR_BAD_VALUE;
    uint32_t want_init = 0;
    if (parse_u32(init->val, init->val_len, &want_init) != 0) return CB_ERR_BAD_VALUE;
    if (want_init >= want_wrap) return CB_ERR_BAD_VALUE; /* explicit, not modulo */
    if (!tag_is_valid(tag->val, tag->val_len)) return CB_ERR_BAD_VALUE;
    if (wmask->val_len != CB_WMASK_BYTES * 2) return CB_ERR_BAD_IMAGE;
    if (cb_hex_to_bytes(wmask->val, wmask->val_len, out->wmask, CB_WMASK_BYTES) != 0)
        return CB_ERR_BAD_IMAGE;
    if (image->val_len != CB_REG_SPACE * 2) return CB_ERR_BAD_IMAGE;
    if (cb_hex_to_bytes(image->val, image->val_len, out->image, CB_REG_SPACE) != 0)
        return CB_ERR_BAD_IMAGE;
    if (cb_crc32(out->image, CB_REG_SPACE) != want_crc) return CB_ERR_CRC;

    memcpy(out->tag, tag->val, tag->val_len);
    out->tag[tag->val_len] = '\0';
    out->crc = want_crc;
    out->flags = (uint8_t)want_flags;
    out->wrap = (uint16_t)want_wrap;
    out->initial = (uint8_t)want_init;
    out->type = CB_CMD_LOAD;
    return CB_OK;
}

static cb_err parse_arm(const char *s, size_t len, cb_cmd *out) {
    kv_token tokens[2];
    int n = split_kv(s, len, tokens, 2);
    if (n != 1 || !key_is(&tokens[0], "lease_ms")) return CB_ERR_BAD_COMMAND;
    uint32_t lease = 0;
    if (parse_u32(tokens[0].val, tokens[0].val_len, &lease) != 0) return CB_ERR_BAD_VALUE;
    if (lease == 0 || lease > CB_LEASE_MAX_MS) return CB_ERR_LEASE_TOO_LONG;
    out->lease_ms = lease;
    out->type = CB_CMD_ARM;
    return CB_OK;
}

static cb_err parse_drain(const char *s, size_t len, cb_cmd *out) {
    kv_token tokens[2];
    int n = split_kv(s, len, tokens, 2);
    if (n != 1 || !key_is(&tokens[0], "n")) return CB_ERR_BAD_COMMAND;
    uint32_t count = 0;
    if (parse_u32(tokens[0].val, tokens[0].val_len, &count) != 0) return CB_ERR_BAD_VALUE;
    if (count == 0 || count > CB_DRAIN_MAX) return CB_ERR_BAD_VALUE;
    out->drain_n = (uint16_t)count;
    out->type = CB_CMD_TRACE_DRAIN;
    return CB_OK;
}

cb_err cb_parse_line(const char *line, cb_cmd *out) {
    memset(out, 0, sizeof(*out));
    if (line == NULL) return CB_ERR_BAD_COMMAND;

    size_t i = 0;
    while (is_space(line[i])) i++;
    const char *rest = line + i;
    size_t rest_len = strlen(rest);

    size_t word_len = 0;
    while (word_len < rest_len && !is_space(rest[word_len])) word_len++;
    int only_spaces_after = 1;
    for (size_t k = word_len; k < rest_len; k++) {
        if (!is_space(rest[k])) { only_spaces_after = 0; break; }
    }

#define WORD_IS(lit) (word_len == sizeof(lit) - 1 && memcmp(rest, lit, word_len) == 0)
    if (WORD_IS("PING") || WORD_IS("STATUS") || WORD_IS("DISARM")) {
        if (!only_spaces_after) return CB_ERR_BAD_COMMAND;
        out->type = WORD_IS("PING") ? CB_CMD_PING
                  : WORD_IS("STATUS") ? CB_CMD_STATUS
                  : CB_CMD_DISARM;
        return CB_OK;
    }
#undef WORD_IS

    if (rest_len > 5 && memcmp(rest, "LOAD ", 5) == 0)
        return parse_load(rest + 5, rest_len - 5, out);
    if (rest_len > 4 && memcmp(rest, "ARM ", 4) == 0)
        return parse_arm(rest + 4, rest_len - 4, out);
    if (rest_len >= 6 && memcmp(rest, "TRACE ", 6) == 0) {
        const char *sub = rest + 6;
        size_t sub_len = rest_len - 6;
        if (sub_len == 3 && memcmp(sub, "END", 3) == 0) {
            out->type = CB_CMD_TRACE_END;
            return CB_OK;
        }
        if (sub_len > 6 && memcmp(sub, "DRAIN ", 6) == 0)
            return parse_drain(sub + 6, sub_len - 6, out);
    }
    return CB_ERR_BAD_COMMAND;
}
