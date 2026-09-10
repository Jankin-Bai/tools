using System;
using System.Diagnostics;

/// <summary>
/// MyTools Launcher — forwards all command-line arguments to powershell.exe.
/// Required because Appx Executable attribute cannot reference paths outside
/// the package directory (no C:\, no ..\). This .exe lives in the win11\
/// package directory and relays everything to powershell.exe.
/// </summary>
internal static class MyToolsLauncher
{
    private static int Main()
    {
        string cmdLine = Environment.CommandLine;

        // Strip our own executable path from the command line.
        // It may be quoted if the path contains spaces.
        string arguments;
        if (cmdLine.StartsWith("\"", StringComparison.Ordinal))
        {
            int endQuote = cmdLine.IndexOf('"', 1);
            arguments = endQuote > 0 ? cmdLine.Substring(endQuote + 1).TrimStart() : "";
        }
        else
        {
            int firstSpace = cmdLine.IndexOf(' ');
            arguments = firstSpace < 0 ? "" : cmdLine.Substring(firstSpace + 1);
        }

        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = arguments,
                UseShellExecute = false
            };
            Process.Start(psi);
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("MyToolsLauncher failed: " + ex.Message);
            return 1;
        }
    }
}
