using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace CaptureMail
{
    internal sealed class TargetWindow
    {
        internal readonly IntPtr Handle;
        internal readonly uint ProcessId;
        internal readonly string Title;

        internal TargetWindow(IntPtr handle, uint processId, string title)
        {
            Handle = handle;
            ProcessId = processId;
            Title = title;
        }

        public override string ToString() => Title;
    }

    internal static class WindowCapture
    {
        // Windows SDK interface IDs and vtable methods used below are from Windows.Graphics.Capture,
        // Windows.Graphics.DirectX.Direct3D11.Interop.h, dxgi.h and d3d11.h.
        private static readonly Guid ItemInterop = new Guid("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356");
        private static readonly Guid CaptureItem = new Guid("79C3F95B-31F7-4EC2-A464-632EF5D30760");
        private static readonly Guid PoolStatics2 = new Guid("589B103F-6BBC-5DF5-A991-02E28B3B66D5");
        private static readonly Guid DxgiDevice = new Guid("54EC77FA-1377-44E6-8C32-88FD5F44C84C");
        private static readonly Guid WinRtDevice = new Guid("A37624AB-8D5F-4650-9D3E-9EAE3D9BC670");
        private static readonly Guid DxgiAccess = new Guid("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1");
        private static readonly Guid Texture2D = new Guid("6F15AAF2-D208-4E89-9AB4-489535D34F9C");
        private static readonly Guid Closable = new Guid("30D5A829-7FA4-4026-83BB-D75BAE4EA99E");
        private static readonly uint OwnPid = GetCurrentProcessId();
        private const int Bgra8 = 87; // DXGI_FORMAT_B8G8R8A8_UNORM

        internal static IReadOnlyList<TargetWindow> List()
        {
            var items = new List<TargetWindow>();
            IntPtr shell = GetShellWindow();
            EnumWindows((handle, unused) =>
            {
                GetWindowThreadProcessId(handle, out uint pid);
                if (handle == shell || pid == OwnPid || pid == 0 || !IsWindowVisible(handle)) return true;
                if (DwmGetWindowAttribute(handle, 14, out int cloaked, sizeof(int)) == 0 && cloaked != 0) return true;
                string title = ReadTitle(handle);
                if (!string.IsNullOrWhiteSpace(title)) items.Add(new TargetWindow(handle, pid, title));
                return true;
            }, IntPtr.Zero);
            items.Sort((a, b) => StringComparer.CurrentCultureIgnoreCase.Compare(a.Title, b.Title));
            return items;
        }

        private static string ReadTitle(IntPtr handle)
        {
            var text = new StringBuilder(GetWindowTextLength(handle) + 1);
            GetWindowText(handle, text, text.Capacity);
            return text.ToString();
        }

        private static void CheckTarget(TargetWindow target)
        {
            if (target == null) throw new InvalidOperationException("Choose a window to capture.");
            GetWindowThreadProcessId(target.Handle, out uint pid);
            if (pid == 0 || pid == OwnPid || pid != target.ProcessId || !IsWindowVisible(target.Handle)
                || !string.Equals(ReadTitle(target.Handle), target.Title, StringComparison.Ordinal))
                throw new InvalidOperationException("The selected window closed or changed its title. Choose it again.");
            if (IsIconic(target.Handle)) throw new InvalidOperationException("Restore the selected window before capturing.");
            if (DwmGetWindowAttribute(target.Handle, 14, out int cloaked, sizeof(int)) == 0 && cloaked != 0)
                throw new InvalidOperationException("Show the selected window on the current desktop.");
            if (GetWindowDisplayAffinity(target.Handle, out uint affinity) && affinity != 0)
                throw new InvalidOperationException("This window restricts screen capture.");
        }

        internal static Task<byte[]> CaptureAsync(TargetWindow target) => CaptureAsync(target, CancellationToken.None);

        internal static Task<byte[]> CaptureAsync(TargetWindow target, CancellationToken token)
        {
            return Task.Run(() =>
            {
                token.ThrowIfCancellationRequested();
                CheckTarget(target);
                Check(RoInitialize(1)); // RO_INIT_MULTITHREADED on this worker.
                try
                {
                    using (var scope = new ComScope())
                    {
                        var timer = Stopwatch.StartNew();
                        IntPtr factory = scope.Factory("Windows.Graphics.Capture.GraphicsCaptureItem", ItemInterop);
                        Guid iid = CaptureItem;
                        IntPtr item = scope.Take(Method<CreateForWindow>(factory, 3)(factory, target.Handle, ref iid, out IntPtr value), value);
                        Check(Method<GetSize>(item, 7)(item, out FrameSize size));
                        if (size.Width < 1 || size.Height < 1) throw new InvalidOperationException("The window has no visible content.");
                        IntPtr device = CreateDevice(scope, out IntPtr context);
                        IntPtr dxgi = scope.Query(device, DxgiDevice);
                        IntPtr wrapped = scope.Take(CreateDirect3D11DeviceFromDXGIDevice(dxgi, out value), value, true);
                        IntPtr projected = scope.Query(wrapped, WinRtDevice);
                        IntPtr pools = scope.Factory("Windows.Graphics.Capture.Direct3D11CaptureFramePool", PoolStatics2);
                        IntPtr pool = scope.Take(Method<CreatePool>(pools, 6)(pools, projected, Bgra8, 1, size, out value), value, true);
                        IntPtr session = scope.Take(Method<CreateSession>(pool, 10)(pool, item, out value), value, true);
                        Check(Method<ActionMethod>(session, 6)(session)); // StartCapture

                        // The free-threaded pool does not require a UI dispatcher. Empty pools return S_OK and null.
                        var nextFrame = Method<GetObject>(pool, 7);
                        do
                        {
                            CheckBudget(timer, token);
                            Check(nextFrame(pool, out value));
                            if (value == IntPtr.Zero) Thread.Sleep(20);
                        } while (value == IntPtr.Zero);
                        IntPtr frame = scope.Take(0, value, true);
                        Check(Method<GetSize>(frame, 8)(frame, out FrameSize actual));
                        if (actual.Width != size.Width || actual.Height != size.Height)
                            throw new InvalidOperationException("The window resized during capture. Try again.");
                        IntPtr surface = scope.Take(Method<GetObject>(frame, 6)(frame, out value), value);
                        IntPtr access = scope.Query(surface, DxgiAccess);
                        iid = Texture2D;
                        IntPtr texture = scope.Take(Method<GetInterface>(access, 3)(access, ref iid, out value), value);
                        byte[] png = EncodeTexture(scope, device, context, texture, actual, timer, token);
                        token.ThrowIfCancellationRequested();
                        CheckTarget(target);
                        return png;
                    }
                }
                finally { RoUninitialize(); }
            }, token);
        }

        private static void CheckBudget(Stopwatch timer, CancellationToken token)
        {
            token.ThrowIfCancellationRequested();
            if (timer.Elapsed >= TimeSpan.FromSeconds(10))
                throw new TimeoutException("Windows did not return the window image in time. Check that the desktop is unlocked.");
        }

        private static IntPtr CreateDevice(ComScope scope, out IntPtr context)
        {
            // D3D_DRIVER_TYPE_HARDWARE, then Microsoft's WARP renderer if the adapter is unavailable.
            int result = D3D11CreateDevice(IntPtr.Zero, 1, IntPtr.Zero, 0x20, IntPtr.Zero, 0, 7,
                out IntPtr device, out _, out context);
            if (result < 0)
                result = D3D11CreateDevice(IntPtr.Zero, 5, IntPtr.Zero, 0x20, IntPtr.Zero, 0, 7,
                    out device, out _, out context);
            scope.Take(result, device);
            scope.Take(result, context);
            return device;
        }

        private static byte[] EncodeTexture(ComScope scope, IntPtr device, IntPtr context, IntPtr texture,
            FrameSize size, Stopwatch timer, CancellationToken token)
        {
            Method<GetTextureDescription>(texture, 10)(texture, out TextureDescription desc);
            if (desc.Format != Bgra8 || desc.Width < size.Width || desc.Height < size.Height
                || desc.SampleCount != 1 || desc.ArraySize != 1 || desc.MipLevels != 1)
                throw new InvalidOperationException("Windows returned an unsupported capture texture.");
            desc.Usage = 3; // D3D11_USAGE_STAGING
            desc.CpuAccess = 0x20000; // D3D11_CPU_ACCESS_READ
            desc.Bind = desc.Misc = 0;
            IntPtr staging = scope.Take(Method<CreateTexture>(device, 5)(device, ref desc, IntPtr.Zero, out IntPtr value), value);
            Method<CopyResource>(context, 47)(context, staging, texture);
            var map = Method<MapResource>(context, 14);
            MappedResource mapped;
            while (true)
            {
                CheckBudget(timer, token);
                int result = map(context, staging, 0, 1, 0x100000, out mapped); // READ, DO_NOT_WAIT
                if (result != unchecked((int)0x887A000A)) { Check(result); break; } // DXGI_ERROR_WAS_STILL_DRAWING
                Thread.Sleep(10);
            }
            try
            {
                using (var pixels = new Bitmap(size.Width, size.Height, checked((int)mapped.RowPitch),
                    PixelFormat.Format32bppPArgb, mapped.Data))
                using (var stream = new MemoryStream())
                {
                    pixels.Save(stream, ImageFormat.Png);
                    return stream.ToArray();
                }
            }
            finally { Method<UnmapResource>(context, 15)(context, staging, 0); }
        }

        private sealed class ComScope : IDisposable
        {
            private readonly List<IntPtr> references = new List<IntPtr>();
            private readonly HashSet<int> closeBeforeRelease = new HashSet<int>();

            internal IntPtr Take(int hr, IntPtr pointer, bool close = false)
            {
                Check(hr);
                if (pointer == IntPtr.Zero) throw new InvalidOperationException("Windows did not provide a capture object.");
                if (close) closeBeforeRelease.Add(references.Count);
                references.Add(pointer);
                return pointer;
            }

            internal IntPtr Query(IntPtr pointer, Guid iid) => Take(Marshal.QueryInterface(pointer, ref iid, out IntPtr result), result);

            internal IntPtr Factory(string name, Guid iid)
            {
                Check(WindowsCreateString(name, name.Length, out IntPtr hstring));
                try { return Take(RoGetActivationFactory(hstring, ref iid, out IntPtr result), result); }
                finally { WindowsDeleteString(hstring); }
            }

            public void Dispose()
            {
                Guid iid = Closable;
                for (int i = references.Count - 1; i >= 0; --i)
                {
                    IntPtr pointer = references[i];
                    if (closeBeforeRelease.Contains(i) && Marshal.QueryInterface(pointer, ref iid, out IntPtr close) >= 0)
                    {
                        try { Method<ActionMethod>(close, 6)(close); }
                        finally { Marshal.Release(close); }
                    }
                    Marshal.Release(pointer);
                }
            }
        }

        private static void Check(int hr) { if (hr < 0) Marshal.ThrowExceptionForHR(hr); }

        private static T Method<T>(IntPtr instance, int slot) where T : Delegate =>
            Marshal.GetDelegateForFunctionPointer<T>(Marshal.ReadIntPtr(Marshal.ReadIntPtr(instance), slot * IntPtr.Size));

        [StructLayout(LayoutKind.Sequential)] private struct FrameSize { internal int Width, Height; }
        [StructLayout(LayoutKind.Sequential)] private struct TextureDescription
        {
            internal uint Width, Height, MipLevels, ArraySize;
            internal int Format;
            internal uint SampleCount, SampleQuality, Usage, Bind, CpuAccess, Misc;
        }
        [StructLayout(LayoutKind.Sequential)] private struct MappedResource { internal IntPtr Data; internal uint RowPitch, DepthPitch; }

        // Platform-default calling convention is stdcall for Windows COM and Win32 callbacks.
        private delegate int CreateForWindow(IntPtr self, IntPtr handle, ref Guid iid, out IntPtr item);
        private delegate int GetSize(IntPtr self, out FrameSize size);
        private delegate int CreatePool(IntPtr self, IntPtr device, int format, int buffers, FrameSize size, out IntPtr pool);
        private delegate int CreateSession(IntPtr self, IntPtr item, out IntPtr session);
        private delegate int GetObject(IntPtr self, out IntPtr value);
        private delegate int ActionMethod(IntPtr self);
        private delegate int GetInterface(IntPtr self, ref Guid iid, out IntPtr value);
        private delegate void GetTextureDescription(IntPtr self, out TextureDescription description);
        private delegate int CreateTexture(IntPtr self, ref TextureDescription description, IntPtr initial, out IntPtr value);
        private delegate void CopyResource(IntPtr self, IntPtr destination, IntPtr source);
        private delegate int MapResource(IntPtr self, IntPtr resource, uint subresource, uint type, uint flags, out MappedResource mapped);
        private delegate void UnmapResource(IntPtr self, IntPtr resource, uint subresource);
        private delegate bool EnumWindow(IntPtr handle, IntPtr unused);

        [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindow callback, IntPtr unused);
        [DllImport("user32.dll")] private static extern IntPtr GetShellWindow();
        [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern bool GetWindowDisplayAffinity(IntPtr hwnd, out uint affinity);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowTextLength(IntPtr hwnd);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr hwnd, StringBuilder title, int length);
        [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
        [DllImport("kernel32.dll")] private static extern uint GetCurrentProcessId();
        [DllImport("dwmapi.dll")] private static extern int DwmGetWindowAttribute(IntPtr hwnd, uint attribute, out int value, int size);
        [DllImport("combase.dll")] private static extern int RoInitialize(int type);
        [DllImport("combase.dll")] private static extern void RoUninitialize();
        [DllImport("combase.dll", CharSet = CharSet.Unicode, ExactSpelling = true)] private static extern int WindowsCreateString(string text, int length, out IntPtr hstring);
        [DllImport("combase.dll")] private static extern int WindowsDeleteString(IntPtr hstring);
        [DllImport("combase.dll")] private static extern int RoGetActivationFactory(IntPtr name, ref Guid iid, out IntPtr factory);
        [DllImport("d3d11.dll")] private static extern int CreateDirect3D11DeviceFromDXGIDevice(IntPtr dxgi, out IntPtr device);
        [DllImport("d3d11.dll")] private static extern int D3D11CreateDevice(IntPtr adapter, int driver, IntPtr software, uint flags,
            IntPtr levels, uint levelCount, uint sdk, out IntPtr device, out uint feature, out IntPtr context);
    }
}
