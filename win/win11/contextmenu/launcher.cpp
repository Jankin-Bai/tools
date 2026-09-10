// Minimal native Win32 launcher for MyTools sparse package.
#include <windows.h>
#include <shellapi.h>

int WINAPI WinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance, LPSTR lpCmdLine, int nCmdShow)
{
    SHELLEXECUTEINFOA sei = {0};
    sei.cbSize = sizeof(sei);
    sei.fMask = SEE_MASK_NOCLOSEPROCESS;
    sei.lpVerb = "open";
    sei.lpFile = "powershell.exe";
    sei.lpParameters = lpCmdLine;
    sei.nShow = SW_SHOWNORMAL;
    ShellExecuteExA(&sei);
    return 0;
}
