/*
 * SyncLight.app CFBundleExecutable.
 *
 * Must be a real Mach-O (not a shell script). macOS Bluetooth / Screen
 * Recording TCC attach to this binary's bundle id
 * (com.robobloq.synclight.gui). We request Screen Recording *before*
 * execve so SyncLight appears in Privacy → Screen Recording; after
 * execve of python the responsible identity often stays SyncLight.
 */
#include <libgen.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <CoreGraphics/CoreGraphics.h>

#ifndef GLASS_PY
#define GLASS_PY "/Users/admin/Projects/synclight/glass_gui.py"
#endif
#ifndef PYTHON_BIN
#define PYTHON_BIN "/usr/bin/python3"
#endif

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;

    /*
     * Never call CGRequestScreenCaptureAccess here — it popped «Запись экрана»
     * on every Dock launch even when SyncLight was already in the list.
     * Screen capture TCC for this Mach-O still needs the SyncLight toggle ON
     * in System Settings; glass_gui falls back / holds last colour if grey.
     */

    const char *py = PYTHON_BIN;
    const char *script = GLASS_PY;
    char *copy = strdup(script);
    if (copy) {
        char *dir = dirname(copy);
        if (dir && dir[0])
            (void)chdir(dir);
        free(copy);
    }
    char *args[] = {(char *)py, (char *)script, NULL};
    execv(py, args);
    perror("SyncLight: execv python");
    return 127;
}
