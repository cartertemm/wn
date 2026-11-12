/* Runs argv[1..] as a child and waits for it, so macOS treats this app as the responsible process. */
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static pid_t child;

static void forward(int signum)
{
	if (child > 0)
		kill(child, signum);
}

int main(int argc, char **argv)
{
	int status;
	int error;

	if (argc < 2) {
		fprintf(stderr, "usage: %s program [argument ...]\n", argv[0]);
		return 64;
	}
	signal(SIGTERM, forward);
	signal(SIGINT, forward);
	signal(SIGHUP, forward);
	error = posix_spawn(&child, argv[1], NULL, NULL, argv + 1, environ);
	if (error != 0) {
		fprintf(stderr, "wn-agent: cannot start %s: %s\n", argv[1], strerror(error));
		return 69;
	}
	while (waitpid(child, &status, 0) < 0) {
		if (errno != EINTR)
			return 70;
	}
	if (WIFSIGNALED(status))
		return 128 + WTERMSIG(status);
	return WEXITSTATUS(status);
}
