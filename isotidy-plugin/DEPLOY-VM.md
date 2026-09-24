# Running IsoTidy when Plant 3D lives in a VM

Short answer: **the same plugin works unchanged — there is no
`isotidy-plugin_vm`, deliberately.**

A VM running Windows + Plant 3D is, from the plugin's point of view,
just another Windows machine.  IsoTidy is an in-process DLL loaded by
AutoCAD itself: it talks only to the AutoCAD API of the process that
loaded it.  It does not care whether that process runs on bare metal, in
VMware, VirtualBox, Hyper-V, or a cloud desktop.  It uses no network, no
external services, no absolute paths.  Maintaining a second copy of the
same code under a different name would only guarantee the two drift
apart — one codebase, deployed wherever Plant 3D is.

What IS different in a VM is *getting the DLL there and building it*.

## A. Build outside, run inside (recommended for the air-gapped VM)

The build machine needs the .NET 10 SDK and the three AutoCAD managed
DLLs.  The VM has the DLLs; the host has the SDK.  So:

1. **Once**: inside the VM, copy the three reference DLLs out to the
   shared folder:

   ```
   C:\Program Files\Autodesk\AutoCAD 2026\acdbmgd.dll
   C:\Program Files\Autodesk\AutoCAD 2026\acmgd.dll
   C:\Program Files\Autodesk\AutoCAD 2026\accoremgd.dll
        -> isotidy-plugin\acad-ref\
   ```

   (Referenced at build time only, never shipped — the csproj already
   looks in `acad-ref\` first.)

2. On the host: `dotnet build -c Release` → `bin\Release\IsoTidy.dll`.

3. Copy **just `IsoTidy.dll`** into the VM (shared folder / drag-drop).

4. In Plant 3D: `NETLOAD` → pick the DLL → type `ISOTIDY`.
   For auto-load on every start, drop the `bundle\` folder into
   `%APPDATA%\Autodesk\ApplicationPlugins\` inside the VM instead.

## B. Build inside the VM

If the VM is allowed one-time media transfer, install the .NET 10 SDK
offline installer inside it, copy the whole `isotidy-plugin\` folder in,
and `dotnet build -c Release` there.  Nothing needs internet: the
project has zero NuGet dependencies, by design, for exactly this
scenario.

## The one real VM gotcha

Windows marks files copied from a host or downloaded as *blocked*
(NTFS zone identifier), and NETLOAD then fails with a security prompt
or `eLoadFailed`.  Fix before loading:

```powershell
Unblock-File <path>\IsoTidy.dll
```

Or right-click the DLL → Properties → Unblock.  That is the entire
difference between a VM deployment and a native one.

## Workflow inside the VM (identical to native)

```
Create Iso → ISOGEN writes the DWG → open it in Plant 3D
  → ISOTIDY            (tidies the open drawing; Ctrl+Z rejects)
  → ISOTIDYSCORE       (dry run: numbers only, nothing changes)
  → ISOTIDYALL         (whole folder → tidied\ copies, originals kept)
```
