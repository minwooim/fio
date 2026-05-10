/*
 * Dump the contents of a verify state file in plain text
 */
#include <sys/types.h>
#include <sys/stat.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include "../log.h"
#include "../os/os.h"
#include "../verify-state.h"
#include "../crc/crc32c.h"
#include "debug.h"

static void show_s(struct thread_io_list *s, unsigned int no_s)
{
	int i;

	printf("Thread:\t\t%u\n", no_s);
	printf("Name:\t\t%s\n", s->name);
	printf("Depth:\t\t%llu\n", (unsigned long long) s->depth);
	printf("Number IOs:\t%llu\n", (unsigned long long) s->numberio);
	printf("Index:\t\t%llu\n", (unsigned long long) s->index);

	printf("Inflight writes:\n");
	if (!s->depth)
		return;
	for (i = s->depth - 1; i >= 0; i--) {
		uint64_t numberio;
		numberio = s->inflight[i].numberio;
		if (numberio == INVALID_NUMBERIO)
			printf("\tNot inflight\n");
		else
			printf("\t%llu\n",
			       (unsigned long long) s->inflight[i].numberio);
	}
}

static void show(struct thread_io_list *s, size_t size)
{
	int no_s;

	no_s = 0;
	do {
		int i;

		s->depth = le32_to_cpu(s->depth);
		s->numberio = le64_to_cpu(s->numberio);
		s->index = le64_to_cpu(s->index);

		for (i = 0; i < s->depth; i++)
			s->inflight[i].numberio = le64_to_cpu(s->inflight[i].numberio);

		show_s(s, no_s);
		no_s++;
		size -= __thread_io_list_sz(s->depth);
		s = (struct thread_io_list *)((char *) s +
			__thread_io_list_sz(s->depth));
	} while (size != 0);
}

static const char *rand_dist_name(uint64_t d)
{
	switch (d) {
	case 0: return "random";
	case 1: return "zipf";
	case 2: return "pareto";
	case 3: return "gauss";
	case 4: return "zoned";
	case 5: return "zoned_abs";
	default: return "unknown";
	}
}

static double u64_bits_to_double(uint64_t v)
{
	double d;
	memcpy(&d, &v, sizeof(d));
	return d;
}

static void show_workload(struct vstate_workload *wl)
{
	uint64_t dist = le64_to_cpu(wl->random_distribution);

	printf("Workload:\n");
	printf("  td_ddir:\t\t%llu\n", (unsigned long long) le64_to_cpu(wl->td_ddir));
	printf("  bs[read]:\t\t%llu\n", (unsigned long long) le64_to_cpu(wl->bs[0]));
	printf("  bs[write]:\t\t%llu\n", (unsigned long long) le64_to_cpu(wl->bs[1]));
	printf("  bs[trim]:\t\t%llu\n", (unsigned long long) le64_to_cpu(wl->bs[2]));
	printf("  size:\t\t\t%llu\n", (unsigned long long) le64_to_cpu(wl->size));
	printf("  io_size:\t\t%llu\n", (unsigned long long) le64_to_cpu(wl->io_size));
	printf("  start_offset:\t\t%llu\n", (unsigned long long) le64_to_cpu(wl->start_offset));
	printf("  offset_increment:\t%llu\n", (unsigned long long) le64_to_cpu(wl->offset_increment));
	printf("  random_distribution:\t%llu (%s)\n", (unsigned long long) dist,
	       rand_dist_name(dist));
	printf("  zipf_theta:\t\t%.6f\n",
	       u64_bits_to_double(le64_to_cpu(wl->zipf_theta)));
	printf("  pareto_h:\t\t%.6f\n",
	       u64_bits_to_double(le64_to_cpu(wl->pareto_h)));
	printf("  random_center:\t%.6f\n",
	       u64_bits_to_double(le64_to_cpu(wl->random_center)));
}

static void show_verify_state(void *buf, size_t size)
{
	struct verify_state_hdr *hdr = buf;
	struct thread_io_list *s;
	uint32_t crc;

	hdr->version = le64_to_cpu(hdr->version);
	hdr->size = le64_to_cpu(hdr->size);
	hdr->crc = le64_to_cpu(hdr->crc);

	printf("Version:\t0x%x\n", (unsigned int) hdr->version);
	printf("Size:\t\t%u\n", (unsigned int) hdr->size);
	printf("CRC:\t\t0x%x\n", (unsigned int) hdr->crc);

	size -= sizeof(*hdr);
	if (hdr->size != size) {
		log_err("Size mismatch\n");
		return;
	}

	s = buf + sizeof(*hdr);
	crc = fio_crc32c((unsigned char *) s, hdr->size);
	if (crc != hdr->crc) {
		log_err("crc mismatch %x != %x\n", crc, (unsigned int) hdr->crc);
		return;
	}

	if (hdr->version == VSTATE_HDR_VERSION) {
		show_workload((struct vstate_workload *) s);
		s = (struct thread_io_list *)((char *)s + sizeof(struct vstate_workload));
		size -= sizeof(struct vstate_workload);
		show(s, size);
	} else {
		log_err("Unsupported version %d\n", (int) hdr->version);
	}
}

static int show_file(const char *file)
{
	struct stat sb;
	void *buf;
	int ret, fd;

	fd = open(file, O_RDONLY);
	if (fd < 0) {
		log_err("open %s: %s\n", file, strerror(errno));
		return 1;
	}

	if (fstat(fd, &sb) < 0) {
		log_err("stat: %s\n", strerror(errno));
		close(fd);
		return 1;
	}

	buf = malloc(sb.st_size);
	ret = read(fd, buf, sb.st_size);
	if (ret < 0) {
		log_err("read: %s\n", strerror(errno));
		close(fd);
		free(buf);
		return 1;
	} else if (ret != sb.st_size) {
		log_err("Short read\n");
		close(fd);
		free(buf);
		return 1;
	}

	close(fd);
	show_verify_state(buf, sb.st_size);

	free(buf);
	return 0;
}

int main(int argc, char *argv[])
{
	int i, ret;

	debug_init();

	if (argc < 2) {
		log_err("Usage: %s <state file>\n", argv[0]);
		return 1;
	}

	ret = 0;
	for (i = 1; i < argc; i++) {
		ret = show_file(argv[i]);
		if (ret)
			break;
	}

	return ret;
}
