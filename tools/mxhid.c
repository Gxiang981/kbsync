/*
 * mxhid — macOS 常驻 HID++ 助手（给 kbsync 使用）
 *
 * 为什么需要它：mxswitch 每次调用都要全量扫描所有 HID collection 并逐槽位探测，
 * 一次要 1~5 秒；而且扫描期间设备可能刚好休眠，导致「写了切换指令但其实没生效」
 * 的假成功。本助手把设备常驻打开，用行命令驱动，单次操作毫秒级。
 *
 * 命令（stdin 一行一条，stdout 回一行 JSON）：
 *   ping        -> {"ok":1,"channel":2,"hosts":3}   读取当前通道（读不到说明设备不在本机/休眠）
 *   switch N    -> {"ok":1}                          把键盘切到通道 N（1..3）
 *   info        -> 同 ping，另外带 product
 *   diag        -> 打印枚举诊断（纯文本）
 *   quit        -> 退出
 *
 * 此外每 keepalive_ms（默认 500ms）自动 ping 一次：既能保持键盘不休眠，
 * 也能第一时间发现键盘被切走（设备不在了就重新扫描）。
 *
 * 编译：
 *   clang -O2 -Wall -Wextra -o mxhid mxhid.c \
 *       -framework IOKit -framework CoreFoundation -framework CoreGraphics
 *
 * 需要给二进制授予「输入监控（Input Monitoring）」权限：./mxhid --setup
 *
 * HID++ 协议部分参考 Logitech HID++ 规范与 mxswitch（MIT, marcocosta97/mxswitch）。
 */

#include <ApplicationServices/ApplicationServices.h>
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/hid/IOHIDDevice.h>
#include <IOKit/hid/IOHIDLib.h>
#include <IOKit/hid/IOHIDManager.h>
#include <mach-o/dyld.h>

#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <libgen.h>
#include <string.h>
#include <sys/select.h>
#include <unistd.h>

#define LOGITECH_VID        0x046D
#define SW_ID               0x0A
#define ROOT_FEATURE        0x00
#define FEAT_CHANGE_HOST    0x1814
#define REPORT_SHORT        0x10
#define REPORT_LONG         0x11
#define INPUT_MONITORING_SETTINGS \
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"

/* 槽位探测顺序：把最常见的 3 放最前（MX 键盘在接收器上通常是槽 3），
 * 这样一次扫描 0.4 秒就能命中，不用等整轮扫完。 */
static const uint8_t DEVICE_INDICES[] = {3, 1, 2, 0xFF, 4, 5, 6};

#define REPLY_IGNORE        0
#define REPLY_MATCH         1
#define REPLY_ERROR         2
#define REPLY_TOO_SHORT     3

static int32_t prop_int(IOHIDDeviceRef dev, CFStringRef key) {
    CFTypeRef v = IOHIDDeviceGetProperty(dev, key);
    int32_t out = 0;
    if (v && CFGetTypeID(v) == CFNumberGetTypeID())
        CFNumberGetValue((CFNumberRef)v, kCFNumberSInt32Type, &out);
    return out;
}

static size_t frame_len(uint8_t report_id) {
    return report_id == REPORT_SHORT ? 7 : 20;
}

static size_t frame_build(uint8_t report_id, uint8_t dev_idx, uint8_t feature_idx,
                          uint8_t function, const uint8_t *params, size_t nparams,
                          uint8_t *out) {
    if (report_id != REPORT_SHORT && report_id != REPORT_LONG) return 0;
    size_t len = frame_len(report_id);
    if (nparams > len - 4) return 0;
    memset(out, 0, len);
    out[0] = report_id;
    out[1] = dev_idx;
    out[2] = feature_idx;
    out[3] = (uint8_t)((function << 4) | SW_ID);
    if (params && nparams) memcpy(out + 4, params, nparams);
    return len;
}

static int classify_reply(const uint8_t *reply, size_t len,
                          uint8_t dev_idx, uint8_t feature_idx,
                          uint8_t function, uint8_t sw_id) {
    if (len < 5) return REPLY_TOO_SHORT;
    if (reply[0] != REPORT_SHORT && reply[0] != REPORT_LONG) return REPLY_IGNORE;
    if (len < frame_len(reply[0])) return REPLY_TOO_SHORT;
    if (reply[1] != dev_idx && !(dev_idx == 0xFF && reply[1] == 0)) return REPLY_IGNORE;
    uint8_t header = (uint8_t)((function << 4) | sw_id);
    if (reply[2] == 0xFF || reply[2] == 0x8F) {
        if (len < 6) return REPLY_TOO_SHORT;
        return reply[3] == feature_idx && reply[4] == header ? REPLY_ERROR : REPLY_IGNORE;
    }
    if (reply[2] != feature_idx) return REPLY_IGNORE;
    if (reply[3] != header) return REPLY_IGNORE;
    return REPLY_MATCH;
}

static uint8_t  g_reply[64];
static CFIndex  g_reply_len;
static int      g_got_reply;
static IOHIDDeviceRef g_reply_dev;

static void input_cb(void *ctx, IOReturn result, void *sender,
                     IOHIDReportType type, uint32_t report_id,
                     uint8_t *report, CFIndex len) {
    (void)ctx; (void)result; (void)type;

    if (len <= 0 || len > (CFIndex)sizeof(g_reply) - 1) goto done;

    /* IOKit 对「report id 是否包含在缓冲区里」并不一致，统一按 HID++ 规范归一化。 */
    if (report_id != 0 && report[0] != (uint8_t)report_id) {
        g_reply[0] = (uint8_t)report_id;
        memcpy(g_reply + 1, report, (size_t)len);
        g_reply_len = len + 1;
    } else {
        memcpy(g_reply, report, (size_t)len);
        g_reply_len = len;
    }
    g_reply_dev = (IOHIDDeviceRef)sender;
    g_got_reply = 1;

done:
    CFRunLoopStop(CFRunLoopGetCurrent());
}

static uint8_t g_next_sw_id = SW_ID;

/* 最近一次写设备是否失败：写失败 = 设备真的走了；写成功但没回包 = 设备在休眠 */
static int g_write_failed = 0;

/* 连续多少次「写成功但没回包」——用来判断句柄是否已经失效 */
static int g_silent = 0;

/* 探测超时：后台自动发现时用短超时（省 CPU），按需探测时用长超时（更稳） */
static double g_probe_timeout = 0.45;

/* 发一帧 HID++ 请求；out == NULL 表示「发了就不管」（setCurrentHost 不回包）。 */
static int hidpp_call(IOHIDDeviceRef dev, uint8_t report_id, uint8_t dev_idx,
                      uint8_t feature_idx, uint8_t function,
                      const uint8_t *params, size_t nparams,
                      uint8_t *out, size_t *out_len, double timeout_s) {
    uint8_t frame[20];
    size_t len = frame_build(report_id, dev_idx, feature_idx, function,
                             params, nparams, frame);
    if (!len) return 0;

    uint8_t sw_id = g_next_sw_id;
    g_next_sw_id = (uint8_t)(8 + ((sw_id - 8 + 1) % 8));
    frame[3] = (uint8_t)((function << 4) | sw_id);

    if (IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, report_id,
                             frame, len) != kIOReturnSuccess) {
        g_write_failed = 1;   /* 写失败 = 设备真的不在了 */
        return 0;
    }
    g_write_failed = 0;
    if (!out) return 1;

    double deadline = CFAbsoluteTimeGetCurrent() + timeout_s;
    while (CFAbsoluteTimeGetCurrent() < deadline) {
        g_got_reply = 0;
        g_reply_len = 0;
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, true);
        if (!g_got_reply || g_reply_len < 5) continue;
        if (g_reply_dev != dev) continue;
        int verdict = classify_reply(g_reply, (size_t)g_reply_len,
                                     dev_idx, feature_idx, function, sw_id);
        if (verdict == REPLY_IGNORE || verdict == REPLY_TOO_SHORT) continue;
        if (verdict == REPLY_ERROR) return 0;
        memcpy(out, g_reply, (size_t)g_reply_len);
        if (out_len) *out_len = (size_t)g_reply_len;
        return 1;
    }
    return 0;
}

static uint8_t resolve_feature(IOHIDDeviceRef dev, uint8_t report_id,
                               uint8_t dev_idx, uint16_t feature_id) {
    uint8_t probe[3] = {(uint8_t)(feature_id >> 8), (uint8_t)feature_id, 0};
    uint8_t reply[64];
    if (!hidpp_call(dev, report_id, dev_idx, ROOT_FEATURE, 0, probe, 3,
                    reply, NULL, g_probe_timeout))
        return 0;
    return reply[4];
}

/* ------------------------------------------------------------------ */
/* 设备管理                                                            */
/* ------------------------------------------------------------------ */

typedef struct {
    IOHIDDeviceRef dev;
    uint8_t  dev_idx;
    uint8_t  report_id;
    uint8_t  feature_idx;
    char     name[128];
} hid_dev_t;

static IOHIDManagerRef g_mgr = NULL;
static hid_dev_t       g_dev;
static int             g_have = 0;
static uint8_t        *g_rxbuf = NULL;

/* 上次成功找到设备的位置：接收器 path + 设备槽位 + report id。
 * 记住它，之后重新发现只要 1 次探测（~0.3s），能在键盘刚切换过来、
 * 还没来得及睡着的那个窗口里把它抓住。 */
static char     g_last_path[512];
static uint8_t  g_last_idx, g_last_rid, g_last_feat;
static int      g_have_last = 0;

static void device_path(IOHIDDeviceRef d, char *out, size_t n) {
    out[0] = 0;
    CFStringRef p = IOHIDDeviceGetProperty(d, CFSTR("DevicePropertyPath"));
    if (p && CFGetTypeID(p) == CFStringGetTypeID())
        CFStringGetCString(p, out, (CFIndex)n, kCFStringEncodingUTF8);
}

/* 把「上次找到设备的位置」存到磁盘：助手重启后第一件事就是直接去那个槽位试，
 * 不用从头扫，键盘刚醒来就能在 0.4 秒内被抓住。 */
static void last_dev_save(void) {
    if (!g_have_last) return;
    const char *home = getenv("HOME");
    if (!home) return;
    char path[1024];
    snprintf(path, sizeof(path), "%s/.kbsync/mxhid-device", home);
    FILE *f = fopen(path, "w");
    if (!f) return;
    fprintf(f, "%s\n%d\n%d\n%d\n", g_last_path, (int)g_last_idx, (int)g_last_rid, (int)g_last_feat);
    fclose(f);
}

static void last_dev_load(void) {
    const char *home = getenv("HOME");
    if (!home) return;
    char path[1024];
    snprintf(path, sizeof(path), "%s/.kbsync/mxhid-device", home);
    FILE *f = fopen(path, "r");
    if (!f) return;
    int idx = 0, rid = 0, feat = 0;
    char p[512];
    p[0] = 0;
    int got = fscanf(f, "%511[^\n]\n%d\n%d\n%d", p, &idx, &rid, &feat);
    fclose(f);
    if (got == 4 && p[0]) {
        snprintf(g_last_path, sizeof(g_last_path), "%s", p);
        g_last_idx = (uint8_t)idx;
        g_last_rid = (uint8_t)rid;
        g_last_feat = (uint8_t)feat;
        g_have_last = 1;
    }
}

static void release_device(void) {
    if (g_have && g_dev.dev) {
        IOHIDDeviceUnscheduleFromRunLoop(g_dev.dev, CFRunLoopGetCurrent(),
                                         kCFRunLoopDefaultMode);
        IOHIDDeviceClose(g_dev.dev, kIOHIDOptionsTypeNone);
    }
    free(g_rxbuf);
    g_rxbuf = NULL;
    g_have = 0;
    memset(&g_dev, 0, sizeof(g_dev));
    if (g_mgr) { CFRelease(g_mgr); g_mgr = NULL; }
}

static int name_is_keyboard(const char *name) {
    if (!name || !*name) return 0;
    return strcasestr(name, "Keys") || strcasestr(name, "Keyboard") ||
           strcasestr(name, "Mechanical") || strcasestr(name, "MX");
}

/*
 * 扫描所有罗技 vendor HID collection，找到支持 ChangeHost 的设备。
 * 键盘优先（名字里带 Keys/Keyboard/Mechanical/MX）；否则取第一个。
 */
static int discover(void) {
    release_device();

    IOHIDManagerRef mgr = IOHIDManagerCreate(kCFAllocatorDefault, kIOHIDOptionsTypeNone);
    if (!mgr) return 0;
    IOHIDManagerSetDeviceMatching(mgr, NULL);
    CFSetRef cset = IOHIDManagerCopyDevices(mgr);
    if (!cset) { CFRelease(mgr); return 0; }

    CFIndex count = CFSetGetCount(cset);
    IOHIDDeviceRef *devices = calloc((size_t)count + 1, sizeof(*devices));
    if (!devices) { CFRelease(cset); CFRelease(mgr); return 0; }
    CFSetGetValues(cset, (const void **)devices);

    IOHIDDeviceRef fallback_dev = NULL;
    uint8_t  fallback_idx = 0, fallback_rid = 0, fallback_feat = 0;
    char     fallback_name[128];
    char     fallback_path[512];

    for (CFIndex i = 0; i < count; i++) {
        IOHIDDeviceRef d = devices[i];
        if (prop_int(d, CFSTR(kIOHIDVendorIDKey)) != LOGITECH_VID) continue;
        int32_t usage_page = prop_int(d, CFSTR(kIOHIDPrimaryUsagePageKey));
        if (usage_page < 0xFF00) continue;       /* 只看 vendor collection */
        int32_t max_in = prop_int(d, CFSTR(kIOHIDMaxInputReportSizeKey));
        if (max_in <= 0 || max_in > 64) max_in = 64;

        if (IOHIDDeviceOpen(d, kIOHIDOptionsTypeNone) != kIOReturnSuccess) continue;
        uint8_t *rxbuf = calloc((size_t)max_in, 1);
        IOHIDDeviceRegisterInputReportCallback(d, rxbuf, (CFIndex)max_in, input_cb, NULL);
        IOHIDDeviceScheduleWithRunLoop(d, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);

        char name[128];
        name[0] = 0;
        CFStringRef product = IOHIDDeviceGetProperty(d, CFSTR(kIOHIDProductKey));
        if (product && CFGetTypeID(product) == CFStringGetTypeID())
            CFStringGetCString(product, name, sizeof(name), kCFStringEncodingUTF8);

        /* 快速路径：先试上次找到它的那个槽位 */
        char path[512];
        device_path(d, path, sizeof(path));
        if (g_have_last && path[0] && !strcmp(path, g_last_path)) {
            uint8_t feat = resolve_feature(d, g_last_rid, g_last_idx, FEAT_CHANGE_HOST);
            if (feat) {
                g_dev.dev = d;
                g_dev.dev_idx = g_last_idx;
                g_dev.report_id = g_last_rid;
                g_dev.feature_idx = feat;
                snprintf(g_dev.name, sizeof(g_dev.name), "%s", name);
                g_rxbuf = rxbuf;
                g_have = 1;
                g_mgr = mgr;
                g_last_feat = feat;
                snprintf(g_last_path, sizeof(g_last_path), "%s", path);
                free(devices);
                CFRelease(cset);
                return 1;
            }
        }

        for (size_t a = 0; a < sizeof(DEVICE_INDICES) / sizeof(DEVICE_INDICES[0]); a++) {
            uint8_t dev_idx = DEVICE_INDICES[a];
            uint8_t rids[2] = {REPORT_LONG, REPORT_SHORT};
            for (int b = 0; b < 2; b++) {
                uint8_t feat = resolve_feature(d, rids[b], dev_idx, FEAT_CHANGE_HOST);
                if (!feat) continue;
                if (name_is_keyboard(name)) {
                    g_dev.dev = d;
                    g_dev.dev_idx = dev_idx;
                    g_dev.report_id = rids[b];
                    g_dev.feature_idx = feat;
                    snprintf(g_dev.name, sizeof(g_dev.name), "%s", name);
                    g_rxbuf = rxbuf;
                    g_have = 1;
                    g_mgr = mgr;
                    /* 记住位置，下次只需 1 次探测 */
                    g_last_idx = dev_idx;
                    g_last_rid = rids[b];
                    g_last_feat = feat;
                    snprintf(g_last_path, sizeof(g_last_path), "%s", path);
                    last_dev_save();
                    free(devices);
                    CFRelease(cset);
                    return 1;
                }
                if (!fallback_dev) {
                    fallback_dev = d;
                    fallback_idx = dev_idx;
                    fallback_rid = rids[b];
                    fallback_feat = feat;
                    snprintf(fallback_name, sizeof(fallback_name), "%s", name);
                    snprintf(fallback_path, sizeof(fallback_path), "%s", path);
                }
                break;
            }
        }

        /* 这个 collection 没有我们要的设备，关掉 */
        IOHIDDeviceUnscheduleFromRunLoop(d, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
        IOHIDDeviceClose(d, kIOHIDOptionsTypeNone);
        free(rxbuf);
    }

    free(devices);
    CFRelease(cset);

    if (fallback_dev) {
        int32_t max_in = prop_int(fallback_dev, CFSTR(kIOHIDMaxInputReportSizeKey));
        if (max_in <= 0 || max_in > 64) max_in = 64;
        uint8_t *rxbuf = calloc((size_t)max_in, 1);
        IOHIDDeviceRegisterInputReportCallback(fallback_dev, rxbuf, (CFIndex)max_in,
                                               input_cb, NULL);
        IOHIDDeviceScheduleWithRunLoop(fallback_dev, CFRunLoopGetCurrent(),
                                       kCFRunLoopDefaultMode);
        g_dev.dev = fallback_dev;
        g_dev.dev_idx = fallback_idx;
        g_dev.report_id = fallback_rid;
        g_dev.feature_idx = fallback_feat;
        snprintf(g_dev.name, sizeof(g_dev.name), "%s", fallback_name);
        g_rxbuf = rxbuf;
        g_have = 1;
        g_mgr = mgr;
        g_last_idx = fallback_idx;
        g_last_rid = fallback_rid;
        g_last_feat = fallback_feat;
        snprintf(g_last_path, sizeof(g_last_path), "%s", fallback_path);
        last_dev_save();
        return 1;
    }

    CFRelease(mgr);
    return 0;
}

/* 设备是否还在本机：用 getFeature 探测。
 * 重要：有些接收器（例如 Mac 上那只）不回复 getHostInfo，只认 getFeature，
 * 所以「存在性」必须用后者判断。 */
static int device_present(void) {
    if (!g_have) return 0;
    uint8_t feat = resolve_feature(g_dev.dev, g_dev.report_id, g_dev.dev_idx,
                                   FEAT_CHANGE_HOST);
    return feat != 0;
}

/* 读当前通道。成功返回 1；*channel 为 1..3，读不到时置 0（=「在，但通道未知」）。
 *
 * 关键：设备可能只是「睡着了」，此时写是成功的、只是没有回包。
 * 所以不要一失败就丢弃句柄重新扫描（既慢又容易错过它醒来的瞬间），
 * 而是保留句柄多试几次；只有写都失败了才认为设备真的离开了。
 */
static int read_channel(int *channel, int *hosts) {
    if (channel) *channel = 0;
    if (hosts) *hosts = 0;
    for (int attempt = 0; attempt < 3; attempt++) {
        if (!g_have && !discover()) return 0;
        uint8_t reply[64];
        size_t rlen = 0;
        if (hidpp_call(g_dev.dev, g_dev.report_id, g_dev.dev_idx, g_dev.feature_idx,
                       0, NULL, 0, reply, &rlen, 0.45)) {
            if (rlen >= 6) {
                if (channel) *channel = (int)reply[5] + 1;
                if (hosts) *hosts = (int)reply[4];
            }
            g_silent = 0;
            return 1;
        }
        if (g_write_failed) break;   /* 设备真的走了 */
        if (++g_silent > 2) {        /* 写成功却一直没回包：句柄多半已失效 */
            g_silent = 0;
            release_device();
            continue;
        }
        if (device_present()) {      /* getHostInfo 不回包，但设备确实在 */
            g_silent = 0;
            return 1;
        }
    }
    release_device();
    return 0;
}

/* 切到指定通道。成功返回 1。 */
static int switch_channel(int channel) {
    if (channel < 1 || channel > 3) return 0;

    int current = 0;
    if (!read_channel(&current, NULL)) return 0;
    if (current == channel) return 1;

    uint8_t params[1];
    params[0] = (uint8_t)(channel - 1);
    uint8_t frame[20];
    size_t len = frame_build(g_dev.report_id, g_dev.dev_idx, g_dev.feature_idx,
                             1, params, 1, frame);
    if (!len) return 0;
    if (IOHIDDeviceSetReport(g_dev.dev, kIOHIDReportTypeOutput, g_dev.report_id,
                             frame, len) != kIOReturnSuccess)
        return 0;

    /* setCurrentHost 不回包：用「设备是否还在 / 通道是否已变」来复核 */
    for (int i = 0; i < 8; i++) {
        usleep(80 * 1000);
        uint8_t reply[64];
        size_t rlen = 0;
        if (hidpp_call(g_dev.dev, g_dev.report_id, g_dev.dev_idx, g_dev.feature_idx,
                       0, NULL, 0, reply, &rlen, 0.2)) {
            if (rlen >= 6 && (int)reply[5] + 1 == channel) return 1;
            continue;
        }
        if (g_write_failed) {
            release_device();
            return 1;   /* 设备离开了本机 = 切换成功 */
        }
        if (!device_present()) {      /* getFeature 也不应答 = 设备真的走了 */
            release_device();
            return 1;
        }
    }
    return 0;
}

/* ------------------------------------------------------------------ */

static int setup_permission(void) {
    char exe[1024];
    uint32_t size = (uint32_t)sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) == 0) {
        char resolved[1024];
        if (realpath(exe, resolved)) snprintf(exe, sizeof(exe), "%s", resolved);
        printf("请在 系统设置 → 隐私与安全性 → 输入监控 中添加并允许下面这个文件：\n");
        printf("  %s\n", exe);
    }
    printf("（点左下角 '+' 选择该文件，然后把它右侧的开关打开）\n");
    fflush(stdout);
    char cmd[256];
    snprintf(cmd, sizeof(cmd), "/usr/bin/open '%s'", INPUT_MONITORING_SETTINGS);
    if (system(cmd) != 0)
        fprintf(stderr, "无法自动打开设置，请手动前往「隐私与安全性 → 输入监控」。\n");
    printf("{\"ok\":0}\n");
    fflush(stdout);
    return 1;
}

static void cmd_diag(void) {
    IOHIDManagerRef mgr = IOHIDManagerCreate(kCFAllocatorDefault, kIOHIDOptionsTypeNone);
    if (!mgr) { printf("diag: no manager\n"); return; }
    IOHIDManagerSetDeviceMatching(mgr, NULL);
    CFSetRef cset = IOHIDManagerCopyDevices(mgr);
    if (!cset) { CFRelease(mgr); printf("diag: no devices\n"); return; }
    CFIndex count = CFSetGetCount(cset);
    IOHIDDeviceRef *devices = calloc((size_t)count + 1, sizeof(*devices));
    CFSetGetValues(cset, (const void **)devices);
    int logi = 0, vendor = 0, opened = 0;
    for (CFIndex i = 0; i < count; i++) {
        IOHIDDeviceRef d = devices[i];
        if (prop_int(d, CFSTR(kIOHIDVendorIDKey)) != LOGITECH_VID) continue;
        logi++;
        int32_t up = prop_int(d, CFSTR(kIOHIDPrimaryUsagePageKey));
        int32_t u = prop_int(d, CFSTR(kIOHIDPrimaryUsageKey));
        char name[96];
        name[0] = 0;
        CFStringRef product = IOHIDDeviceGetProperty(d, CFSTR(kIOHIDProductKey));
        if (product && CFGetTypeID(product) == CFStringGetTypeID())
            CFStringGetCString(product, name, sizeof(name), kCFStringEncodingUTF8);
        printf("logi dev: up=0x%04x u=0x%04x name=%s\n", (int)up, (int)u, name);
        if (up < 0xFF00) continue;
        vendor++;
        IOReturn orc = IOHIDDeviceOpen(d, kIOHIDOptionsTypeNone);
        printf("  vendor collection: open=%d\n", (int)orc);
        if (orc != kIOReturnSuccess) continue;
        opened++;
        int32_t max_in = prop_int(d, CFSTR(kIOHIDMaxInputReportSizeKey));
        if (max_in <= 0 || max_in > 64) max_in = 64;
        uint8_t *rxbuf = calloc((size_t)max_in, 1);
        IOHIDDeviceRegisterInputReportCallback(d, rxbuf, (CFIndex)max_in, input_cb, NULL);
        IOHIDDeviceScheduleWithRunLoop(d, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
        for (size_t a = 0; a < sizeof(DEVICE_INDICES) / sizeof(DEVICE_INDICES[0]); a++) {
            uint8_t dev_idx = DEVICE_INDICES[a];
            uint8_t rids[2] = {REPORT_LONG, REPORT_SHORT};
            for (int b = 0; b < 2; b++) {
                uint8_t feat = resolve_feature(d, rids[b], dev_idx, FEAT_CHANGE_HOST);
                printf("  probe idx=0x%02x rid=0x%02x feat=0x%02x\n",
                       (int)dev_idx, (int)rids[b], (int)feat);
                if (feat) break;
            }
        }
        IOHIDDeviceUnscheduleFromRunLoop(d, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
        IOHIDDeviceClose(d, kIOHIDOptionsTypeNone);
        free(rxbuf);
    }
    printf("diag: total=%d logitech=%d vendor=%d opened=%d\n",
           (int)count, logi, vendor, opened);
    free(devices);
    CFRelease(cset);
    CFRelease(mgr);
}

static void handle_command(char *line) {
    char cmd[32];
    int arg = 0;
    cmd[0] = 0;
    int n = sscanf(line, "%31s %d", cmd, &arg);
    if (n < 1) return;

    if (!strcmp(cmd, "ping") || !strcmp(cmd, "info")) {
        int channel = 0, hosts = 0;
        /* 没有设备时直接回答「没有」，不触发全量扫描（扫描由后台循环负责），
           这样调用方（kbsync）可以高频轮询而不拖慢。 */
        int ok = g_have ? read_channel(&channel, &hosts) : 0;
        if (ok) {
            if (!strcmp(cmd, "info"))
                printf("{\"ok\":1,\"channel\":%d,\"hosts\":%d,\"product\":\"%s\"}\n",
                       channel, hosts, g_dev.name);
            else
                printf("{\"ok\":1,\"channel\":%d,\"hosts\":%d}\n", channel, hosts);
        } else {
            printf("{\"ok\":0}\n");
        }
        fflush(stdout);
    } else if (!strcmp(cmd, "switch")) {
        printf("{\"ok\":%d}\n", switch_channel(arg));
        fflush(stdout);
    } else if (!strcmp(cmd, "diag")) {
        cmd_diag();
        fflush(stdout);
    } else if (!strcmp(cmd, "quit")) {
        printf("{\"ok\":1}\n");
        fflush(stdout);
        release_device();
        exit(0);
    } else {
        printf("{\"ok\":0,\"error\":\"unknown command\"}\n");
        fflush(stdout);
    }
}

int main(int argc, char **argv) {
    long keepalive_us = 500 * 1000;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--setup")) return setup_permission();
        if (!strcmp(argv[i], "--probe-timeout-ms") && i + 1 < argc) {
            long ms = strtol(argv[++i], NULL, 10);
            if (ms < 50) ms = 50;
            g_probe_timeout = (double)ms / 1000.0;
        }
        if (!strcmp(argv[i], "--keepalive-ms") && i + 1 < argc) {
            long ms = strtol(argv[++i], NULL, 10);
            if (ms < 100) ms = 100;
            keepalive_us = ms * 1000;
        }
    }

    last_dev_load();
    setvbuf(stdout, NULL, _IOLBF, 0);

    char line[256];
    fd_set fds;
    struct timeval tv;
    double next_discover = 0.0;

    for (;;) {
        FD_ZERO(&fds);
        FD_SET(STDIN_FILENO, &fds);
        tv.tv_sec = 0;
        tv.tv_usec = (suseconds_t)keepalive_us;
        int r = select(STDIN_FILENO + 1, &fds, NULL, NULL, &tv);
        if (r < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (r > 0 && FD_ISSET(STDIN_FILENO, &fds)) {
            if (!fgets(line, sizeof(line), stdin)) break;   /* EOF -> 退出 */
            handle_command(line);
        } else {
            /* 保活：戳一下设备，让它别休眠；顺便发现设备已被切走 */
            if (g_have) {
                uint8_t reply[64];
                size_t rlen = 0;
                if (!hidpp_call(g_dev.dev, g_dev.report_id, g_dev.dev_idx,
                                g_dev.feature_idx, 0, NULL, 0, reply, &rlen, 0.2)) {
                    if (g_write_failed) {
                        release_device();          /* 真的走了 */
                    } else {
                        /* 写成功但没回包：设备可能休眠，也可能句柄已经失效
                           （键盘切走又切回后，旧的 IOHIDDeviceRef 不再可靠）。
                           连着几次没回包就丢弃句柄、重新打开，避免卡死。 */
                        if (++g_silent > 3) {
                            g_silent = 0;
                            release_device();
                        }
                    }
                } else {
                    g_silent = 0;
                }
            } else if (CFAbsoluteTimeGetCurrent() >= next_discover) {
                /* 当前没有设备：每隔几秒自己找一次，键盘一醒就接管 */
                next_discover = CFAbsoluteTimeGetCurrent() + 2.0;
                discover();
            }
        }
    }

    release_device();
    return 0;
}
