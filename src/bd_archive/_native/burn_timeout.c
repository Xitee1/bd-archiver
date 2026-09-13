/* Linux SG_IO write timeout interposer. Loaded only for a single burn process. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <scsi/sg.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

static int (*original_ioctl)(int, unsigned long, ...);
static dev_t target_device;
static unsigned int minimum_ms;
static pid_t owner_pid;

static void fail(const char *message)
{
    dprintf(STDERR_FILENO, "[burn-timeout] %s\n", message);
    _exit(125);
}

static unsigned long setting(const char *name, unsigned long maximum)
{
    const char *value = getenv(name);
    char *end;
    if (!value || !*value || strspn(value, "0123456789") != strlen(value))
        fail("Missing or invalid helper configuration; refusing to burn.");
    errno = 0;
    unsigned long result = strtoul(value, &end, 10);
    if (errno || *end || result > maximum)
        fail("Helper configuration is out of range; refusing to burn.");
    return result;
}

__attribute__((constructor)) static void initialize(void)
{
    original_ioctl = dlsym(RTLD_NEXT, "ioctl");
    if (!original_ioctl || dlsym(RTLD_DEFAULT, "ioctl") != (void *)ioctl)
        fail("Cannot intercept ioctl; refusing to burn.");
    unsigned long seconds = setting("BD_BURN_TIMEOUT_SECONDS", 86400);
    if (!seconds)
        fail("Write timeout must be positive; refusing to burn.");
    minimum_ms = (unsigned int)seconds * 1000;
    unsigned int major_id = setting("BD_BURN_DEVICE_MAJOR", UINT_MAX);
    unsigned int minor_id = setting("BD_BURN_DEVICE_MINOR", UINT_MAX);
    target_device = makedev(major_id, minor_id);
    owner_pid = getpid();
    int probe = getenv("BD_BURN_TIMEOUT_PROBE") != NULL;

    /* The loader has mapped both files. Do not leak their handles or preload
       configuration into mkisofs or other programs executed by growisofs. */
    close((int)setting("BD_BURN_LIBRARY_FD", INT_MAX));
    close((int)setting("BD_BURN_EXECUTABLE_FD", INT_MAX));
    unsetenv("LD_PRELOAD");
    unsetenv("BD_BURN_TIMEOUT_SECONDS");
    unsetenv("BD_BURN_DEVICE_MAJOR");
    unsetenv("BD_BURN_DEVICE_MINOR");
    unsetenv("BD_BURN_TIMEOUT_PROBE");
    unsetenv("BD_BURN_LIBRARY_FD");
    unsetenv("BD_BURN_EXECUTABLE_FD");

    if (probe) {
        /* Exit before growisofs main: no drive access or backend invocation. */
        dprintf(STDOUT_FILENO, "bd-archive-timeout-v1:%u:%u:%u\n",
                minimum_ms, major_id, minor_id);
        _exit(0);
    }
}

int ioctl(int fd, unsigned long request, ...)
{
    va_list arguments;
    va_start(arguments, request);
    void *argument = va_arg(arguments, void *);
    va_end(arguments);
    int saved_errno = errno;

    if (getpid() == owner_pid && request == SG_IO && argument) {
        sg_io_hdr_t *header = argument;
        struct stat info;
        if (header->interface_id == 'S' && header->cmdp && header->cmd_len >= 10 &&
            header->dxfer_direction == SG_DXFER_TO_DEV &&
            (header->cmdp[0] == 0x2a || header->cmdp[0] == 0xaa ||
             header->cmdp[0] == 0x2e) &&
            fstat(fd, &info) == 0 && S_ISBLK(info.st_mode) &&
            info.st_rdev == target_device) {
            unsigned int timeout = header->timeout;
            if (timeout < minimum_ms)
                header->timeout = minimum_ms;
            errno = saved_errno;
            int result = original_ioctl(fd, request, argument);
            int result_errno = errno;
            /* Retain all kernel output fields, and restore the caller's input. */
            header->timeout = timeout;
            errno = result_errno;
            return result;
        }
    }
    errno = saved_errno;
    return original_ioctl(fd, request, argument);
}
