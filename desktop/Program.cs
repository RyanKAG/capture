using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Mail;
using System.Security;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Win32;

namespace CaptureMail
{
    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            bool firstInstance;
            using (var instance = new System.Threading.Mutex(false, @"Local\CaptureMail.F3BEA795", out firstInstance))
            {
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                if (!firstInstance)
                {
                    MessageBox.Show("Window Screenshot Mailer is already open in this Windows session.",
                        "Window Screenshot Mailer", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return;
                }
                Application.Run(new CaptureForm());
            }
        }
    }

    internal sealed class CaptureForm : Form
    {
        private const string SettingsKey = @"Software\CaptureMail";
        private readonly Label account = new Label();
        private readonly Button connect = MakeButton("Sign in");
        private readonly ComboBox windows = MakeSelector();
        private readonly Button refresh = MakeButton("Refresh");
        private readonly TextBox recipient = new TextBox();
        private readonly ComboBox mode = MakeSelector();
        private readonly NumericUpDown minutes = new NumericUpDown {
            Minimum = 0.1m, Maximum = 525600m, Value = 30, DecimalPlaces = 1, Width = 90
        };
        private readonly Label units = MakeLabel("minutes");
        private readonly TextBox times = new TextBox { Text = "09:00, 17:30", Width = 150 };
        private readonly Button preview = MakeButton("Preview");
        private readonly Button test = MakeButton("Send test");
        private readonly Button start = MakeButton("Start schedule");
        private readonly Label status = new Label { Dock = DockStyle.Fill, AutoEllipsis = true };
        private readonly Timer timer = new Timer { Interval = 500 };
        private readonly Stopwatch elapsed = Stopwatch.StartNew();
        private readonly Dictionary<int, DateTime> lastDailyRuns = new Dictionary<int, DateTime>();
        private SortedSet<int> dailySlots = new SortedSet<int>();
        private TimeSpan interval;
        private TimeSpan nextInterval;
        private bool running;
        private bool busy;
        private bool sending;
        private bool closing;
        private System.Threading.CancellationTokenSource activeCapture;
        private string sender = "";
        private string lastTitle = "";

        internal CaptureForm()
        {
            Text = "Window Screenshot Mailer";
            Font = new Font("Segoe UI", 10F);
            AutoScaleMode = AutoScaleMode.Font;
            ClientSize = new Size(620, 370);
            MinimumSize = new Size(545, 360);
            StartPosition = FormStartPosition.CenterScreen;

            var grid = new TableLayoutPanel {
                Dock = DockStyle.Fill, Padding = new Padding(16), ColumnCount = 3, RowCount = 6
            };
            grid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            grid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            for (int i = 0; i < 5; ++i)
                grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            grid.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            Controls.Add(grid);

            account.Dock = DockStyle.Fill;
            account.TextAlign = ContentAlignment.MiddleLeft;
            account.AutoEllipsis = true;
            windows.Dock = DockStyle.Fill;
            recipient.Dock = DockStyle.Fill;
            grid.Controls.Add(MakeLabel("Hotmail / Outlook.com"), 0, 0);
            grid.Controls.Add(account, 1, 0);
            grid.Controls.Add(connect, 2, 0);
            grid.Controls.Add(MakeLabel("Window"), 0, 1);
            grid.Controls.Add(windows, 1, 1);
            grid.Controls.Add(refresh, 2, 1);
            grid.Controls.Add(MakeLabel("Send to"), 0, 2);
            grid.Controls.Add(recipient, 1, 2);
            grid.SetColumnSpan(recipient, 2);
            grid.Controls.Add(MakeLabel("Schedule"), 0, 3);
            var schedule = MakeFlow();
            mode.Items.AddRange(new object[] { "Every", "Daily at" });
            mode.Width = 95;
            mode.SelectedIndex = 0;
            schedule.Controls.AddRange(new Control[] { mode, minutes, units, times });
            grid.Controls.Add(schedule, 1, 3);
            grid.SetColumnSpan(schedule, 2);
            var actions = MakeFlow();
            actions.Padding = new Padding(0, 10, 0, 10);
            actions.Controls.AddRange(new Control[] { preview, test, start });
            grid.Controls.Add(actions, 0, 4);
            grid.SetColumnSpan(actions, 3);
            grid.Controls.Add(status, 0, 5);
            grid.SetColumnSpan(status, 3);

            LoadSettings();
            connect.Click += async (s, e) => await SignInAsync();
            refresh.Click += (s, e) => RefreshWindows();
            mode.SelectedIndexChanged += (s, e) => UpdateControls();
            preview.Click += async (s, e) => await CaptureAsync(true, false);
            test.Click += async (s, e) => await CaptureAsync(false, false);
            start.Click += (s, e) => ToggleSchedule();
            timer.Tick += async (s, e) => await CheckScheduleAsync();
            Shown += (s, e) => RefreshWindows();
            status.Text = "Choose a window to preview. Sign in with the Microsoft account you want to send from.";
            UpdateControls();
        }

        private static Button MakeButton(string text)
        {
            return new Button { Text = text, AutoSize = true, Padding = new Padding(4, 2, 4, 2) };
        }

        private static Label MakeLabel(string text)
        {
            return new Label { Text = text, AutoSize = true, Anchor = AnchorStyles.Left,
                Margin = new Padding(3, 6, 8, 6) };
        }

        private static ComboBox MakeSelector()
        {
            return new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, IntegralHeight = false };
        }

        private static FlowLayoutPanel MakeFlow()
        {
            return new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, WrapContents = true };
        }

        private void UpdateControls()
        {
            bool editable = !busy && !running && !closing;
            account.Text = sender.Length == 0 ? "Sign-in required" : sender;
            connect.Text = sender.Length == 0 ? "Sign in" : "Sign in again";
            connect.Enabled = refresh.Enabled = windows.Enabled = recipient.Enabled = editable;
            mode.Enabled = minutes.Enabled = times.Enabled = editable;
            minutes.Visible = units.Visible = mode.SelectedIndex == 0;
            times.Visible = mode.SelectedIndex == 1;
            preview.Enabled = test.Enabled = editable;
            start.Enabled = !closing && (running || !busy);
            start.Text = running ? "Stop schedule" : "Start schedule";
        }

        private void RefreshWindows()
        {
            string previous = windows.SelectedItem?.ToString() ?? lastTitle;
            try
            {
                var entries = WindowCapture.List();
                windows.Items.Clear();
                foreach (var entry in entries)
                    windows.Items.Add(entry);
                var matches = entries.Where(w => w.ToString() == previous).Take(2).ToList();
                if (matches.Count == 1)
                    windows.SelectedItem = matches[0];
                if (entries.Count == 0)
                    status.Text = "Open the window you want to capture, then click Refresh.";
            }
            catch (Exception ex) { status.Text = ex.Message; }
        }

        private async Task SignInAsync()
        {
            if (busy || running) return;
            busy = true;
            UpdateControls();
            status.Text = "Preparing Microsoft sign-in. Complete the sign-in window when it opens.";
            try
            {
                string connected = await GraphMail.ConnectAsync();
                if (closing) return;
                sender = connected;
                status.Text = "Signed in. Choose a window and enter the recipient's email address.";
                SaveSettings();
            }
            catch (Exception ex)
            {
                if (closing) return;
                sender = "";
                status.Text = ex.Message;
            }
            finally { FinishWork(); }
        }

        private TargetWindow SelectedTarget()
        {
            var target = windows.SelectedItem as TargetWindow;
            if (target == null)
                throw new InvalidOperationException("Select a window. Click Refresh if you just opened it.");
            return target;
        }

        private string RecipientAddress()
        {
            if (sender.Length == 0)
                throw new InvalidOperationException("Click Sign in and connect the Microsoft account you want to send from.");
            string address = recipient.Text.Trim();
            if (address.Length > 0)
            {
                try
                {
                    var parsed = new MailAddress(address);
                    if (parsed.Address == address && parsed.Host.Contains("."))
                        return address;
                }
                catch (FormatException) { }
            }
            throw new InvalidOperationException("Enter one complete recipient email address, such as name@gmail.com.");
        }

        private void ToggleSchedule()
        {
            if (running)
            {
                StopSchedule();
                status.Text = sending ? "Schedule stopped. The current email is being submitted." : "Schedule stopped.";
                return;
            }
            if (busy || closing) return;
            try
            {
                SelectedTarget();
                RecipientAddress();
                if (mode.SelectedIndex == 1)
                    dailySlots = ParseDailyTimes(times.Text);
                interval = TimeSpan.FromMinutes((double)minutes.Value);
                nextInterval = elapsed.Elapsed;
                lastDailyRuns.Clear();
                SaveSettings();
                running = true;
                UpdateControls();
                status.Text = mode.SelectedIndex == 0 ? "Starting interval schedule…" :
                    "Daily schedule active at " + times.Text + " (local time).";
                timer.Start();
                _ = CheckScheduleAsync();
            }
            catch (Exception ex) { status.Text = ex.Message; }
        }

        internal static SortedSet<int> ParseDailyTimes(string text)
        {
            var result = new SortedSet<int>();
            foreach (string part in text.Split(','))
            {
                DateTime value;
                if (!DateTime.TryParseExact(part.Trim(), new[] { "H:mm", "HH:mm" },
                    CultureInfo.InvariantCulture, DateTimeStyles.None, out value))
                    throw new FormatException("Enter 24-hour local times separated by commas, such as 09:00, 17:30.");
                result.Add(value.Hour * 60 + value.Minute);
            }
            return result;
        }

        private async Task CheckScheduleAsync()
        {
            if (!running || busy || closing) return;
            if (mode.SelectedIndex == 0)
            {
                if (elapsed.Elapsed < nextInterval) return;
                nextInterval = elapsed.Elapsed + interval;
            }
            else
            {
                var now = DateTime.Now;
                int slot = now.Hour * 60 + now.Minute;
                DateTime lastDate;
                if (!dailySlots.Contains(slot) ||
                    (lastDailyRuns.TryGetValue(slot, out lastDate) && lastDate >= now.Date)) return;
                lastDailyRuns[slot] = now.Date;
            }
            await CaptureAsync(false, true);
            if (running && mode.SelectedIndex == 0 && elapsed.Elapsed >= nextInterval)
                nextInterval = elapsed.Elapsed + interval;
        }

        private async Task CaptureAsync(bool previewOnly, bool scheduled)
        {
            if (busy || closing || (scheduled != running)) return;
            busy = true;
            bool capturing = false;
            UpdateControls();
            try
            {
                var target = SelectedTarget();
                string address = previewOnly ? "" : RecipientAddress();
                SaveSettings();
                status.Text = "Capturing " + target + "…";
                capturing = true;
                activeCapture = new System.Threading.CancellationTokenSource();
                byte[] png = await WindowCapture.CaptureAsync(target, activeCapture.Token);
                capturing = false;
                if (closing || (scheduled && !running)) return;
                if (previewOnly)
                {
                    ShowPreview(png);
                    status.Text = "Preview complete. Click Send test to email one screenshot.";
                    return;
                }
                sending = true;
                status.Text = "Submitting one screenshot to " + address + "…";
                await GraphMail.SendAsync(sender, address, target.ToString(), png);
                if (closing) return;
                status.Text = "Submitted to " + address + " at " + DateTime.Now.ToString("T") + ". " +
                    (scheduled ? (running ? "Schedule active." : "Schedule stopped.") : "Check the recipient's inbox.");
            }
            catch (Exception ex)
            {
                if (closing || (scheduled && !running && !sending)) return;
                if (scheduled && capturing)
                {
                    status.Text = "Capture skipped. Schedule active. " + ex.Message;
                    return;
                }
                if (scheduled) StopSchedule();
                status.Text = (scheduled ? "Schedule stopped. " : "") + ex.Message +
                    (sending ? " Check Sent Items before trying again." : "");
            }
            finally { FinishWork(); }
        }

        private void ShowPreview(byte[] png)
        {
            using (var stream = new MemoryStream(png, false))
            using (var picture = Image.FromStream(stream))
            using (var viewer = new Form())
            {
                var area = Screen.FromControl(this).WorkingArea;
                viewer.Text = "Screenshot preview";
                viewer.StartPosition = FormStartPosition.CenterParent;
                viewer.ClientSize = new Size(Math.Min(picture.Width, area.Width * 4 / 5),
                    Math.Min(picture.Height, area.Height * 4 / 5));
                viewer.Controls.Add(new PictureBox { Image = picture, SizeMode = PictureBoxSizeMode.Zoom,
                    Dock = DockStyle.Fill, BackColor = Color.DimGray });
                viewer.ShowDialog(this);
            }
        }

        private void StopSchedule()
        {
            timer.Stop();
            running = false;
            activeCapture?.Cancel();
            UpdateControls();
        }

        private void FinishWork()
        {
            activeCapture?.Dispose();
            activeCapture = null;
            sending = busy = false;
            if (IsDisposed) return;
            if (closing) Close();
            else UpdateControls();
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            closing = true;
            StopSchedule();
            if (sending && e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;
                status.Text = "Finishing the current submission, then closing.";
            }
            else
            {
                SaveSettings();
                timer.Dispose();
            }
            base.OnFormClosing(e);
        }

        private void LoadSettings()
        {
            try
            {
                using (var key = Registry.CurrentUser.OpenSubKey(SettingsKey))
                {
                    if (key == null) return;
                    recipient.Text = key.GetValue("Recipient") as string ?? "";
                    lastTitle = key.GetValue("Window") as string ?? "";
                    times.Text = key.GetValue("DailyTimes") as string ?? times.Text;
                    decimal saved;
                    if (decimal.TryParse(key.GetValue("Minutes") as string, NumberStyles.Number,
                        CultureInfo.InvariantCulture, out saved) && saved >= minutes.Minimum && saved <= minutes.Maximum)
                        minutes.Value = saved;
                    mode.SelectedIndex = Object.Equals(key.GetValue("Mode"), "daily") ? 1 : 0;
                }
            }
            catch (Exception ex) when (ex is IOException || ex is SecurityException || ex is UnauthorizedAccessException) { }
        }

        private void SaveSettings()
        {
            try
            {
                using (var key = Registry.CurrentUser.CreateSubKey(SettingsKey))
                {
                    if (key == null) return;
                    key.SetValue("Recipient", recipient.Text.Trim());
                    key.SetValue("Window", windows.SelectedItem?.ToString() ?? lastTitle);
                    key.SetValue("DailyTimes", times.Text);
                    key.SetValue("Mode", mode.SelectedIndex == 1 ? "daily" : "interval");
                    key.SetValue("Minutes", minutes.Value.ToString(CultureInfo.InvariantCulture));
                }
            }
            catch (Exception ex) when (ex is IOException || ex is SecurityException || ex is UnauthorizedAccessException) { }
        }
    }
}
