/* Simulated kernel boundary: no optical device is opened by these tests. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <scsi/sg.h>
#include <stdarg.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>

unsigned int observed_timeout;
unsigned char observed_opcode;
void *observed_buffer;
unsigned int observed_length;

int fstat(int fd, struct stat *info)
{
    int (*real_fstat)(int, struct stat *) = dlsym(RTLD_NEXT, "fstat");
    int result = real_fstat(fd, info);
    if (!result && S_ISCHR(info->st_mode) && info->st_rdev == makedev(1, 3))
        info->st_mode = S_IFBLK | 0600; /* /dev/null stands in for the burner. */
    return result;
}

int ioctl(int fd, unsigned long request, ...)
{
    (void)fd;
    va_list arguments;
    va_start(arguments, request);
    void *argument = va_arg(arguments, void *);
    va_end(arguments);
    if (request != SG_IO) {
        errno = ENOTTY;
        return -1;
    }
    sg_io_hdr_t *header = argument;
    observed_timeout = header->timeout;
    observed_opcode = header->cmdp[0];
    observed_buffer = header->dxferp;
    observed_length = header->dxfer_len;
    header->status = 2;
    header->host_status = 3;
    header->resid = 7;
    errno = EIO;
    return -1;
}
