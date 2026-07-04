// Focal single-file edition.
// Carries the whole portable folder as an embedded zip. First run (or a new version)
// unpacks it to %LOCALAPPDATA%\Focal with a progress bar, then hands off to the inner
// Focal.exe launcher; later runs skip straight to launch. Files the app creates next to
// itself (generated certificates) are NOT in the payload, so upgrades never wipe them —
// the iPhone keeps trusting the same CA.
//
// Build (tools/build_single.py stamps __VERSION__ and embeds the zip):
//   csc /target:winexe /win32icon:assets\icon.ico /res:Focal-Portable.zip,payload.zip
//       /r:System.IO.Compression.dll /r:System.Windows.Forms.dll SingleExe.cs
using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Windows.Forms;

static class Program
{
    const string Version = "__VERSION__";        // stamped by the build script
    const string Prefix = "Focal-Portable/";  // top-level folder inside the payload zip

    [STAThread]
    static int Main(string[] args)
    {
        string root = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Focal");
        string stamp = Path.Combine(root, "version.txt");
        string inner = Path.Combine(root, "Focal.exe");
        bool extractOnly = Array.IndexOf(args, "--extract-only") >= 0;

        bool need = !File.Exists(inner) || !File.Exists(stamp)
                    || File.ReadAllText(stamp).Trim() != Version;
        if (need)
        {
            try
            {
                Extract(root);
                File.WriteAllText(stamp, Version);
            }
            catch (Exception ex)
            {
                MessageBox.Show("Could not unpack Focal:\r\n" + ex.Message +
                    "\r\n\r\nIf Focal is already running, close it and try again.",
                    "Focal", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
        }
        if (extractOnly) return 0;
        Process.Start(new ProcessStartInfo(inner) { WorkingDirectory = root, UseShellExecute = false });
        return 0;
    }

    static void Extract(string root)
    {
        Directory.CreateDirectory(root);
        string rootFull = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar)
                          + Path.DirectorySeparatorChar;
        using (var zs = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
        using (var zip = new ZipArchive(zs, ZipArchiveMode.Read))
        {
            var form = new Form
            {
                Text = "Focal — unpacking (first run only)…",
                Width = 430, Height = 120, FormBorderStyle = FormBorderStyle.FixedDialog,
                MaximizeBox = false, MinimizeBox = false,
                StartPosition = FormStartPosition.CenterScreen, TopMost = true
            };
            var bar = new ProgressBar { Left = 15, Top = 18, Width = 385, Height = 22,
                                        Minimum = 0, Maximum = zip.Entries.Count };
            var lbl = new Label { Left = 15, Top = 48, Width = 385, Text = "Unpacking…" };
            form.Controls.Add(bar); form.Controls.Add(lbl);
            form.Show(); Application.DoEvents();
            int n = 0;
            foreach (var e in zip.Entries)
            {
                n++;
                string rel = e.FullName.Replace('\\', '/');
                if (rel.StartsWith(Prefix)) rel = rel.Substring(Prefix.Length);
                if (rel.Length == 0 || rel.EndsWith("/")) continue;
                string dest = Path.GetFullPath(Path.Combine(root,
                    rel.Replace('/', Path.DirectorySeparatorChar)));
                if (!dest.StartsWith(rootFull, StringComparison.OrdinalIgnoreCase))
                    continue;                      // zip-slip guard
                Directory.CreateDirectory(Path.GetDirectoryName(dest));
                e.ExtractToFile(dest, true);
                if (n % 40 == 0)
                {
                    bar.Value = n;
                    lbl.Text = n + " / " + zip.Entries.Count + " files";
                    Application.DoEvents();
                }
            }
            form.Close();
        }
    }
}
