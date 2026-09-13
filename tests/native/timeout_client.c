#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <scsi/sg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <unistd.h>

extern unsigned int observed_timeout;
extern unsigned char observed_opcode;
extern void *observed_buffer;
extern unsigned int observed_length;

static void check(int fd, int opcode, int direction, unsigned int before,
                  unsigned int expected, int interface, int length)
{
    unsigned char command[16] = {0};
    char payload[32] = "unchanged payload";
    command[0] = opcode;
    command[5] = 42;
    sg_io_hdr_t header = {0};
    header.interface_id = interface;
    header.cmdp = command;
    header.cmd_len = length;
    header.dxferp = payload;
    header.dxfer_len = sizeof(payload);
    header.dxfer_direction = direction;
    header.timeout = before;
    assert(ioctl(fd, SG_IO, &header) == -1 && errno == EIO);
    assert(observed_timeout == expected && header.timeout == before);
    assert(observed_opcode == opcode && command[5] == 42);
    assert(observed_buffer == payload && observed_length == sizeof(payload));
    assert(!strcmp(payload, "unchanged payload"));
    assert(header.status == 2 && header.host_status == 3 && header.resid == 7);
}

int main(int argc, char **argv)
{
    /* A probe that is ignored must never look like a successful activation. */
    if (argc > 1 && !strcmp(argv[1], "-version")) {
        puts("uninterposed client");
        return 0;
    }
    assert(!getenv("LD_PRELOAD") && !getenv("BD_BURN_TIMEOUT_SECONDS"));
    int burner = open("/dev/null", O_RDWR);
    int other = open("/dev/zero", O_RDWR);
    assert(burner >= 0 && other >= 0);
    assert(argc == 2);
    unsigned int minimum = strtoul(argv[1], NULL, 10);
    check(burner, 0x2a, SG_DXFER_TO_DEV, 60000, minimum, 'S', 10);
    check(burner, 0x2a, SG_DXFER_TO_DEV, 0, minimum, 'S', 10);
    check(burner, 0xaa, SG_DXFER_TO_DEV, 60000, minimum, 'S', 12);
    check(burner, 0x2e, SG_DXFER_TO_DEV, 60000, minimum, 'S', 10);
    check(burner, 0x2a, SG_DXFER_TO_DEV, 900000, 900000, 'S', 10);
    check(burner, 0x2a, SG_DXFER_TO_DEV, ~0u, ~0u, 'S', 10);
    check(other, 0x2a, SG_DXFER_TO_DEV, 60000, 60000, 'S', 10);
    check(burner, 0x28, SG_DXFER_FROM_DEV, 60000, 60000, 'S', 10);
    check(burner, 0x35, SG_DXFER_NONE, 60000, 60000, 'S', 10);
    check(burner, 0x2a, SG_DXFER_FROM_DEV, 60000, 60000, 'S', 10);
    check(burner, 0x2a, SG_DXFER_TO_DEV, 60000, 60000, 'X', 10);
    check(burner, 0x2a, SG_DXFER_TO_DEV, 60000, 60000, 'S', 6);
    assert(ioctl(burner, 12345ul, NULL) == -1 && errno == ENOTTY);
    pid_t child = fork();
    assert(child >= 0);
    if (!child) {
        check(burner, 0x2a, SG_DXFER_TO_DEV, 60000, 60000, 'S', 10);
        execl("/bin/sh", "sh", "-c", "test -z \"$LD_PRELOAD\"", NULL);
        _exit(1);
    }
    int status;
    assert(waitpid(child, &status, 0) == child && status == 0);
    close(burner);
    close(other);
    puts("timeout interception verified");
    return 0;
}
