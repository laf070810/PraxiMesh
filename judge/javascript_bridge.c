/* Trusted JSON/function bridge. Learner compilation and execution stay in Wasm.
 * No quickjs-libc, modules, native bytecode, filesystem, or host execution API.
 * Output is serialized here, never via learner-controlled JSON/toJSON/getters.
 */
#include <math.h>
#include <malloc.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "quickjs.h"

#define INPUT_LIMIT 256000
#define OUTPUT_LIMIT 32768
#define HEAP_LIMIT (64 * 1024 * 1024)
#define DEPTH_LIMIT 32
#define NODE_LIMIT 20000
#define ARGS_LIMIT 64
#define SAFE_INTEGER 9007199254740991.0

static int oom;
static size_t usable(const void *ptr) { return ptr ? malloc_usable_size((void *)ptr) : 0; }
static void *allocate(JSMallocState *s, size_t size) {
    if (!size) return NULL;
    if (size > s->malloc_limit || s->malloc_size > s->malloc_limit - size) {
        oom = 1; return NULL;
    }
    void *ptr = malloc(size);
    if (!ptr) { oom = 1; return NULL; }
    size_t actual = usable(ptr);
    if (actual > s->malloc_limit || s->malloc_size > s->malloc_limit - actual) {
        free(ptr); oom = 1; return NULL;
    }
    s->malloc_size += actual;
    s->malloc_count++;
    return ptr;
}
static void release(JSMallocState *s, void *ptr) {
    if (!ptr) return;
    s->malloc_size -= usable(ptr);
    s->malloc_count--;
    free(ptr);
}
static void *resize(JSMallocState *s, void *ptr, size_t size) {
    if (!ptr) return allocate(s, size);
    if (!size) { release(s, ptr); return NULL; }
    /* Allocate-copy-free preserves exact accounting on failure. Peak usage is
       deliberately included in the memory budget. No in-place overflow math. */
    void *next = allocate(s, size);
    if (!next) return NULL;
    size_t old = usable(ptr);
    memcpy(next, ptr, old < size ? old : size);
    release(s, ptr);
    return next;
}

static JSClassID object_class, array_class;
static JSValue object_prototype, array_prototype;
static JSAtom length_atom;

typedef struct {
    char *data;
    size_t size, limit;
    unsigned nodes;
    int error; /* 2 = non-JSON value, 5 = output budget */
    void *ancestors[DEPTH_LIMIT + 1];
} Writer;

static void append(Writer *w, const char *text, size_t size) {
    if (w->error) return;
    if (size > w->limit - w->size) { w->error = 5; return; }
    if (w->data) memcpy(w->data + w->size, text, size);
    w->size += size;
}
static void literal(Writer *w, const char *text) { append(w, text, strlen(text)); }
static void unicode_escape(Writer *w, unsigned value) {
    char escape[7];
    snprintf(escape, sizeof(escape), "\\u%04x", value);
    append(w, escape, 6);
}
static void string_value(JSContext *ctx, Writer *w, JSValueConst value) {
    size_t size;
    const unsigned char *text = (const unsigned char *)JS_ToCStringLen2(ctx, &size, value, 1);
    if (!text) { w->error = 2; return; }
    literal(w, "\"");
    for (size_t i = 0; i < size && !w->error;) {
        unsigned c = text[i++];
        if (c == '"' || c == '\\') {
            char pair[2] = {'\\', (char)c}; append(w, pair, 2);
        } else if (c < 32) {
            unicode_escape(w, c);
        } else if (c < 128) {
            char byte = c; append(w, &byte, 1);
        } else {
            unsigned remaining;
            if ((c & 0xe0) == 0xc0) { c &= 31; remaining = 1; }
            else if ((c & 0xf0) == 0xe0) { c &= 15; remaining = 2; }
            else if ((c & 0xf8) == 0xf0) { c &= 7; remaining = 3; }
            else { w->error = 2; break; }
            if (remaining > size - i) { w->error = 2; break; }
            while (remaining--) {
                unsigned next = text[i++];
                if ((next & 0xc0) != 0x80) { w->error = 2; break; }
                c = (c << 6) | (next & 63);
            }
            if (c <= 0xffff) unicode_escape(w, c);
            else if (c <= 0x10ffff) {
                c -= 0x10000;
                unicode_escape(w, 0xd800 + (c >> 10));
                unicode_escape(w, 0xdc00 + (c & 1023));
            } else w->error = 2;
        }
    }
    literal(w, "\"");
    JS_FreeCString(ctx, (const char *)text);
}
static void free_descriptor(JSContext *ctx, JSPropertyDescriptor *desc) {
    JS_FreeValue(ctx, desc->value);
    JS_FreeValue(ctx, desc->getter);
    JS_FreeValue(ctx, desc->setter);
}
static int own_data(JSContext *ctx, JSValueConst value, JSAtom key, JSPropertyDescriptor *desc) {
    int found = JS_GetOwnProperty(ctx, desc, value, key);
    if (found != 1) return 0;
    if ((desc->flags & JS_PROP_GETSET) || !(desc->flags & JS_PROP_ENUMERABLE)) {
        free_descriptor(ctx, desc); return 0;
    }
    return 1;
}
static void json_value(JSContext *ctx, Writer *w, JSValueConst value, unsigned depth) {
    if (w->error) return;
    if (depth > DEPTH_LIMIT || ++w->nodes > NODE_LIMIT) { w->error = 2; return; }
    if (JS_IsNull(value)) { literal(w, "null"); return; }
    if (JS_IsBool(value)) { literal(w, JS_ToBool(ctx, value) ? "true" : "false"); return; }
    if (JS_IsString(value)) { string_value(ctx, w, value); return; }
    if (JS_IsNumber(value)) {
        double number;
        if (JS_ToFloat64(ctx, &number, value) < 0 || !isfinite(number) ||
            (trunc(number) == number && fabs(number) > SAFE_INTEGER)) { w->error = 2; return; }
        char text[32];
        int size = snprintf(text, sizeof(text), "%.17g", number == 0 ? 0 : number);
        if (size < 0 || size >= (int)sizeof(text)) { w->error = 2; return; }
        append(w, text, size); return;
    }
    if (!JS_IsObject(value)) { w->error = 2; return; }
    /* Direct class read precedes ALL object operations: no Proxy traps. */
    JSClassID class_id = JS_GetClassID(value);
    if (class_id != object_class && class_id != array_class) { w->error = 2; return; }
    void *pointer = JS_VALUE_GET_PTR(value);
    for (unsigned i = 0; i < depth; i++)
        if (w->ancestors[i] == pointer) { w->error = 2; return; }
    w->ancestors[depth] = pointer;
    JSValue prototype = JS_GetPrototype(ctx, value);
    JSValue expected = class_id == array_class ? array_prototype : object_prototype;
    int plain = JS_IsNull(prototype) || (JS_IsObject(prototype) &&
        JS_VALUE_GET_PTR(prototype) == JS_VALUE_GET_PTR(expected));
    JS_FreeValue(ctx, prototype);
    if (!plain) { w->error = 2; return; }
    JSPropertyEnum *keys = NULL;
    uint32_t count = 0;
    if (JS_GetOwnPropertyNames(ctx, &keys, &count, value, JS_GPN_STRING_MASK | JS_GPN_SYMBOL_MASK) < 0) {
        w->error = 2; return;
    }
    for (uint32_t i = 0; i < count; i++) {
        JSValue key = JS_AtomToValue(ctx, keys[i].atom);
        if (!JS_IsString(key)) w->error = 2;
        JS_FreeValue(ctx, key);
    }
    if (class_id == array_class && !w->error) {
        JSPropertyDescriptor length;
        uint32_t size = 0;
        if (JS_GetOwnProperty(ctx, &length, value, length_atom) != 1) w->error = 2;
        else {
            if (JS_ToUint32(ctx, &size, length.value) < 0 || size > NODE_LIMIT || count != size + 1)
                w->error = 2;
            free_descriptor(ctx, &length);
        }
        literal(w, "[");
        for (uint32_t i = 0; i < size && !w->error; i++) {
            JSAtom key = JS_NewAtomUInt32(ctx, i);
            JSPropertyDescriptor desc;
            if (!own_data(ctx, value, key, &desc)) w->error = 2;
            else {
                if (i) literal(w, ",");
                json_value(ctx, w, desc.value, depth + 1);
                free_descriptor(ctx, &desc);
            }
            JS_FreeAtom(ctx, key);
        }
        literal(w, "]");
    } else if (!w->error) {
        literal(w, "{");
        for (uint32_t i = 0; i < count && !w->error; i++) {
            JSPropertyDescriptor desc;
            if (!own_data(ctx, value, keys[i].atom, &desc)) { w->error = 2; break; }
            JSValue key = JS_AtomToValue(ctx, keys[i].atom);
            if (i) literal(w, ",");
            string_value(ctx, w, key);
            literal(w, ":");
            json_value(ctx, w, desc.value, depth + 1);
            JS_FreeValue(ctx, key);
            free_descriptor(ctx, &desc);
        }
        literal(w, "}");
    }
    JS_FreePropertyEnum(ctx, keys, count);
}

int main(void) {
    static char input[INPUT_LIMIT + 1], output[OUTPUT_LIMIT];
    size_t size = fread(input, 1, INPUT_LIMIT + 1, stdin);
    if (size > INPUT_LIMIT || ferror(stdin)) return 70;
    input[size] = 0;
    const JSMallocFunctions allocator = {allocate, release, resize, usable};
    JSRuntime *runtime = JS_NewRuntime2(&allocator, NULL);
    if (!runtime) return 4;
    JS_SetMemoryLimit(runtime, HEAP_LIMIT);
    JS_SetMaxStackSize(runtime, 512 * 1024);
    JSContext *ctx = JS_NewContext(runtime);
    if (!ctx) { JS_FreeRuntime(runtime); return 4; }
    JSValue object = JS_NewObject(ctx), array = JS_NewArray(ctx);
    object_class = JS_GetClassID(object); array_class = JS_GetClassID(array);
    object_prototype = JS_GetPrototype(ctx, object); array_prototype = JS_GetPrototype(ctx, array);
    JS_FreeValue(ctx, object); JS_FreeValue(ctx, array);
    length_atom = JS_NewAtom(ctx, "length");
    JSValue request = JS_ParseJSON(ctx, input, size, "request.json");
    JSValue source = JS_GetPropertyStr(ctx, request, "source");
    JSValue function = JS_GetPropertyStr(ctx, request, "function");
    JSValue args = JS_GetPropertyStr(ctx, request, "args");
    JSValue code = JS_UNDEFINED, global = JS_UNDEFINED, callable = JS_UNDEFINED, result = JS_UNDEFINED;
    JSValue argv[ARGS_LIMIT];
    unsigned argc = 0;
    const char *source_text = NULL, *function_text = NULL;
    int status = 70;
    if (!JS_IsString(source) || !JS_IsString(function) || JS_GetClassID(args) != array_class) goto done;
    size_t source_size, function_size;
    source_text = JS_ToCStringLen(ctx, &source_size, source);
    function_text = JS_ToCStringLen(ctx, &function_size, function);
    if (!source_text || !function_text || !function_size || function_size > 128) goto done;
    for (size_t i = 0; i < function_size; i++) {
        unsigned c = (unsigned char)function_text[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_' || c == '$' ||
              (i && c >= '0' && c <= '9'))) goto done;
    }
    JSValue length = JS_GetProperty(ctx, args, length_atom);
    uint32_t args_size = 0;
    int converted = JS_ToUint32(ctx, &args_size, length);
    JS_FreeValue(ctx, length);
    if (converted < 0 || args_size > ARGS_LIMIT) goto done;
    Writer validation = {.limit = INPUT_LIMIT};
    json_value(ctx, &validation, args, 0);
    if (validation.error) goto done;
    while (argc < args_size) { argv[argc] = JS_GetPropertyUint32(ctx, args, argc); argc++; }
    code = JS_Eval(ctx, source_text, source_size, "solution.js", JS_EVAL_TYPE_GLOBAL | JS_EVAL_FLAG_COMPILE_ONLY);
    if (JS_IsException(code)) { status = 3; goto done; }
    result = JS_EvalFunction(ctx, code); code = JS_UNDEFINED;
    if (JS_IsException(result)) { status = 2; goto done; }
    JS_FreeValue(ctx, result); result = JS_UNDEFINED;
    global = JS_GetGlobalObject(ctx);
    callable = JS_GetPropertyStr(ctx, global, function_text);
    if (JS_IsException(callable) || !JS_IsFunction(ctx, callable)) { status = 2; goto done; }
    result = JS_Call(ctx, callable, JS_UNDEFINED, argc, argv);
    if (JS_IsException(result)) { status = 2; goto done; }
    Writer writer = {.data = output, .limit = OUTPUT_LIMIT};
    literal(&writer, "{\"value\":");
    json_value(ctx, &writer, result, 0);
    literal(&writer, "}");
    status = writer.error;
    if (!status && !oom && fwrite(output, 1, writer.size, stdout) != writer.size) status = 70;
 done:
    if (oom) status = 4;
    if (source_text) JS_FreeCString(ctx, source_text);
    if (function_text) JS_FreeCString(ctx, function_text);
    for (unsigned i = 0; i < argc; i++) JS_FreeValue(ctx, argv[i]);
    JS_FreeValue(ctx, result); JS_FreeValue(ctx, callable); JS_FreeValue(ctx, global); JS_FreeValue(ctx, code);
    JS_FreeValue(ctx, source); JS_FreeValue(ctx, function); JS_FreeValue(ctx, args); JS_FreeValue(ctx, request);
    JS_FreeValue(ctx, object_prototype); JS_FreeValue(ctx, array_prototype); JS_FreeAtom(ctx, length_atom);
    JS_FreeContext(ctx); JS_FreeRuntime(runtime);
    return status;
}
