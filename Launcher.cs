// Focal portable launcher (WinExe, no console window).
// Runs the bundled Python GUI from the exe's own folder; generates the local
// HTTPS certificate on first run. Mirrors Start-Focal.bat.
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

static class Launcher
{
    [STAThread]
    static void Main()
    {
        string baseDir = AppDomain.CurrentDomain.BaseDirectory;
        string py       = Path.Combine(baseDir, "python", "python.exe");
        string pyw      = Path.Combine(baseDir, "python", "pythonw.exe");
        string app      = Path.Combine(baseDir, "app", "app.py");
        string cert     = Path.Combine(baseDir, "app", "server", "cert.pem");
        string makeCert = Path.Combine(baseDir, "app", "server", "make_cert.py");

        if (!File.Exists(pyw) || !File.Exists(app))
        {
            MessageBox.Show("Bundled Python not found.\nRe-extract the whole ZIP, then try again.",
                "Focal", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        try
        {
            // first run: generate the local certificate (auto-detects the LAN IP)
            if (!File.Exists(cert) && File.Exists(makeCert) && File.Exists(py))
            {
                var gen = new ProcessStartInfo(py, "\"" + makeCert + "\"")
                {
                    WorkingDirectory = baseDir,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                };
                using (var p = Process.Start(gen)) { p.WaitForExit(); }
            }
            var psi = new ProcessStartInfo(pyw, "\"" + app + "\"")
            {
                WorkingDirectory = baseDir,
                UseShellExecute = false,
            };
            Process.Start(psi);
        }
        catch (Exception ex)
        {
            MessageBox.Show("Could not start Focal:\n" + ex.Message,
                "Focal", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
