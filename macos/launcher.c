/* The executable inside "AI Radio Host.app": runs the app's venv Python *inside this process*.
 *
 * It must not exec another binary (a shell script that execs Python, say): macOS then can't tie
 * the process to the bundle and the app is missing from Cmd+Tab. So libpython is loaded here,
 * found at run time through the venv's python symlink, which keeps the app working when
 * Homebrew upgrades Python.
 *
 * Build (see build-app.sh): cc -DVENV='"<venv dir, may start with ~/>"' launcher.c
 *                              -Wl,-rpath,<homebrew prefix>/lib
 * The rpath is needed because GTK's typelibs dlopen bare library names (libgobject-2.0.0.dylib).
 */
#include <dlfcn.h>
#include <libgen.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef VENV
#error "define VENV, the app's virtualenv directory"
#endif
#ifndef HOMEBREW_PREFIX
#define HOMEBREW_PREFIX "/opt/homebrew"
#endif

static void fail(const char *what, const char *detail) {
    fprintf(stderr, "AI Radio Host: %s: %s\n", what, detail);
    exit(1);
}

int main(int argc, char **argv) {
    const char *home = getenv("HOME");
    char venv[PATH_MAX], python[PATH_MAX], script[PATH_MAX], real[PATH_MAX], lib[PATH_MAX];
    char path[PATH_MAX * 2];

    if (strncmp(VENV, "~/", 2) == 0)
        snprintf(venv, sizeof venv, "%s/%s", home ? home : "", &VENV[2]);
    else
        snprintf(venv, sizeof venv, "%s", VENV);
    snprintf(python, sizeof python, "%s/bin/python", venv);
    snprintf(script, sizeof script, "%s/bin/radiohost-gui", venv);

    /* .../Python.framework/Versions/3.x/bin/python3.x -> .../Versions/3.x/Python */
    if (!realpath(python, real))
        fail("app not installed (no Python in its venv)", python);
    snprintf(lib, sizeof lib, "%s/Python", dirname(dirname(real)));
    void *py = dlopen(lib, RTLD_NOW | RTLD_GLOBAL);
    if (!py)
        fail("can't load Python", dlerror());
    int (*py_main)(int, char **) = (int (*)(int, char **))dlsym(py, "Py_BytesMain");
    if (!py_main)
        fail("can't start Python", dlerror());

    /* Finder-launched apps don't get the shell PATH; the app runs Homebrew tools and osascript. */
    snprintf(path, sizeof path, HOMEBREW_PREFIX "/bin:" HOMEBREW_PREFIX "/sbin:%s/.local/bin:"
             "/usr/bin:/bin:/usr/sbin:/sbin", home ? home : "");
    setenv("PATH", path, 1);
    setenv("__PYVENV_LAUNCHER__", python, 1);   /* makes Python use the venv */

    char *args[64] = {python, script};
    int n = 2;
    for (int i = 1; i < argc && n < 63; i++)
        args[n++] = argv[i];
    args[n] = NULL;
    return py_main(n, args);
}
