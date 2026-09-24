// The commands the engineer actually types.
//
//   ISOTIDY       tidy the CURRENT drawing.  One undo step: Ctrl+Z rejects.
//   ISOTIDYSCORE  dry run -- score the current drawing, change nothing.
//   ISOTIDYALL    batch a folder of DWGs headlessly (side databases);
//                 originals are NEVER overwritten -- results go to .\tidied\
//
// Every number printed is measured by the same detector the tests use, and
// the guard refuses to certify a drawing whose layers we cannot read: a
// false PASS is worse than no tool.

using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;

[assembly: CommandClass(typeof(IsoTidy.Commands))]

namespace IsoTidy;

public class Commands
{
    static LayerRoles Roles => LayerRoles.Jp1071();

    [CommandMethod("ISOTIDY", CommandFlags.Modal)]
    public void Tidy() => Run(apply: true);

    [CommandMethod("ISOTIDYSCORE", CommandFlags.Modal)]
    public void Score() => Run(apply: false);

    /// Stage log next to the drawing (<name>.isotidy.log).  AutoCAD's
    /// command line does not repaint while a modal command runs, so when a
    /// sheet "does not respond" the ONLY way to know which stage it is in is
    /// a file written as it goes.  Send this file with the drawing.
    sealed class Diag : IDisposable
    {
        readonly StreamWriter _w;
        readonly DateTime _t0 = DateTime.Now;
        public Diag(Database db)
        {
            string path;
            try
            {
                var fn = db.Filename;
                path = string.IsNullOrEmpty(fn)
                    ? Path.Combine(Path.GetTempPath(), "isotidy.log")
                    : Path.ChangeExtension(fn, ".isotidy.log");
                _w = new StreamWriter(path, append: false) { AutoFlush = true };
            }
            catch { _w = null; }
            Log("start");
        }
        public void Log(string stage)
        {
            try
            {
                _w?.WriteLine($"{DateTime.Now:HH:mm:ss.fff}  +{(DateTime.Now - _t0).TotalSeconds,7:F1}s  {stage}");
            }
            catch { }
        }
        public void Dispose() { Log("end"); _w?.Dispose(); }
    }

    /// Run one stage; a crash inside it is logged and reported, never a
    /// silent hang or an AutoCAD exception dialog with no context.
    static T Stage<T>(Diag d, Editor ed, string name, Func<T> f, T fallback)
    {
        d.Log(name + " ...");
        try
        {
            var r = f();
            d.Log(name + " done");
            return r;
        }
        catch (System.Exception ex)
        {
            d.Log($"{name} FAILED: {ex}");
            ed.WriteMessage($"\n!! stage '{name}' failed: {ex.Message} (see .isotidy.log)");
            return fallback;
        }
    }

    void Run(bool apply)
    {
        var doc = Application.DocumentManager.MdiActiveDocument;
        if (doc == null) return;
        var ed = doc.Editor;
        var cfg = new Tuning();

        using var _lock = doc.LockDocument();
        using var tr = doc.Database.TransactionManager.StartTransaction();
        using var diag = new Diag(doc.Database);

        var scene = Stage(diag, ed, "extract",
            () => Extract.FromDatabase(doc.Database, tr, Roles, cfg), null);
        if (scene == null) { tr.Abort(); return; }
        if (scene.Labels.Count == 0)
        {
            ed.WriteMessage(
                "\n!! NO MANAGED ANNOTATIONS FOUND -- refusing to report a score." +
                "\n   Text was found on unmapped layers: " +
                string.Join(", ", scene.UnmappedText
                    .OrderByDescending(kv => kv.Value)
                    .Take(8).Select(kv => $"{kv.Key} ({kv.Value})")) +
                "\n   Fix: map them in LayerRoles (src/Config.cs).\n");
            tr.Abort();
            return;
        }

        var before = Detector.Detect(scene, cfg);
        ed.WriteMessage($"\n{before.Summary("BEFORE")}\n");
        diag.Log($"before: {before.OverlapArea:F2} mm2, {scene.Labels.Count} labels");

        // Price the DESIGN DATA table before anything may move (the
        // title-block blind spot, CONTEXT 12b) -- then clean the leaders
        // (doglegs / own-runs) so the solver sees true geometry.
        int tables = Stage(diag, ed, "reserve tables",
            () => Polish.ReserveTitleBlockTables(tr, scene, doc.Database), 0);
        var (doglegs, ownRuns) = apply
            ? Stage(diag, ed, "leader hygiene",
                    () => Polish.LeaderHygiene(tr, scene, cfg), (0, 0))
            : (0, 0);
        int realigned = apply
            ? Stage(diag, ed, "realign", () => Polish.Realign(tr, scene, doc.Database, cfg), 0) : 0;
        int claims = apply
            ? Stage(diag, ed, "claims", () => Polish.Claims(tr, scene, doc.Database, cfg), 0) : 0;

        var stats = Stage(diag, ed, "solve", () => Solver.Solve(scene, cfg), null);
        if (stats == null) { tr.Abort(); return; }
        var after = Detector.Detect(scene, cfg);
        diag.Log($"solved: {after.OverlapArea:F2} mm2 planned, {stats.Passes} passes");

        if (!apply)
        {
            ed.WriteMessage($"\n{after.Summary("ACHIEVABLE (dry run -- nothing written)")}\n");
            tr.Abort();
            return;
        }

        var (moved, leaders) = Stage(diag, ed, "writeback",
            () => Writeback.Apply(doc.Database, tr, scene, Roles, cfg), (0, 0));
        int frames = Stage(diag, ed, "frames", () => Polish.FixFrames(tr, scene, doc.Database, cfg), 0);
        int threads = Stage(diag, ed, "threads", () => Polish.Threads(tr, scene, doc.Database, cfg), 0);
        int hairlines = Stage(diag, ed, "hairlines", () => Polish.Hairlines(tr, scene, doc.Database, cfg), 0);

        // AUDIT WHAT SHIPS, NOT WHAT WAS PLANNED.  `after` above is the
        // solver's in-memory intention; the entities may disagree (a
        // writeback bug on 2026-09-08 doubled every attribute move while
        // this printed "99% reduced" over a wrecked sheet).  Re-read the
        // drawing exactly as a checker would and gate on THAT: worse than
        // before => nothing is written, and the message says so.
        var shipped = Stage(diag, ed, "re-measure written drawing",
            () => Detector.Detect(Extract.FromDatabase(doc.Database, tr, Roles, cfg), cfg), null);
        if (shipped == null) { tr.Abort(); return; }
        diag.Log($"shipped: {shipped.OverlapArea:F2} mm2");
        if (shipped.OverlapArea > before.OverlapArea + 1e-6)
        {
            tr.Abort();
            ed.WriteMessage(
                $"\n!! ISOTIDY REFUSED: the written result measures WORSE " +
                $"({before.OverlapArea:F2} -> {shipped.OverlapArea:F2} mm2)." +
                "\n   Nothing was changed.  Please report this drawing.\n");
            return;
        }
        tr.Commit();      // one transaction => one undo step: Ctrl+Z rejects all

        ed.WriteMessage($"\n{shipped.Summary("AFTER (measured on the written drawing)")}\n");
        double pct = before.OverlapArea > 0
            ? 100.0 * (before.OverlapArea - shipped.OverlapArea) / before.OverlapArea : 0;
        ed.WriteMessage(
            $"\n    passes {stats.Passes}   labels moved {moved}   leaders {leaders}" +
            $"\n    doglegs fixed {doglegs}   own-runs {ownRuns}   " +
            $"stacks re-aligned {realigned}   orphan leaders claimed {claims}   " +
            $"frames {frames}" +
            $"\n    leaders re-routed {threads}   grazes cleared {hairlines}   " +
            $"tables reserved {tables}" +
            $"\n    OVERLAP REDUCED {pct:F1}%  " +
            $"({before.OverlapArea:F2} -> {shipped.OverlapArea:F2} mm2)" +
            "\n    Ctrl+Z once rejects the whole tidy-up.\n");
    }

    [CommandMethod("ISOTIDYALL", CommandFlags.Modal | CommandFlags.Session)]
    public void TidyAll()
    {
        var doc = Application.DocumentManager.MdiActiveDocument;
        var ed = doc.Editor;
        var res = ed.GetString("\nFolder with DWGs to tidy: ");
        if (res.Status != PromptStatus.OK) return;
        var dir = res.StringResult.Trim('"');
        if (!Directory.Exists(dir))
        {
            ed.WriteMessage($"\nNot a folder: {dir}\n");
            return;
        }
        var outDir = Path.Combine(dir, "tidied");
        Directory.CreateDirectory(outDir);
        var cfg = new Tuning();

        foreach (var dwg in Directory.GetFiles(dir, "*.dwg").OrderBy(f => f))
        {
            try
            {
                using var db = new Database(false, true);
                db.ReadDwgFile(dwg, FileShare.Read, true, null);
                using (var tr = db.TransactionManager.StartTransaction())
                {
                    var scene = Extract.FromDatabase(db, tr, Roles, cfg);
                    if (scene.Labels.Count == 0)
                    {
                        ed.WriteMessage($"\n  {Path.GetFileName(dwg)}: no managed " +
                                        "annotations -- skipped (layer mismatch?)");
                        tr.Abort();
                        continue;
                    }
                    var before = Detector.Detect(scene, cfg);
                    Polish.ReserveTitleBlockTables(tr, scene, db);
                    Polish.LeaderHygiene(tr, scene, cfg);
                    Polish.Realign(tr, scene, db, cfg);
                    Polish.Claims(tr, scene, db, cfg);
                    Solver.Solve(scene, cfg);
                    Writeback.Apply(db, tr, scene, Roles, cfg);
                    Polish.FixFrames(tr, scene, db, cfg);
                    Polish.Threads(tr, scene, db, cfg);
                    Polish.Hairlines(tr, scene, db, cfg);
                    // audit what ships (see Run): worse => not saved
                    var shipped = Detector.Detect(
                        Extract.FromDatabase(db, tr, Roles, cfg), cfg);
                    if (shipped.OverlapArea > before.OverlapArea + 1e-6)
                    {
                        tr.Abort();
                        ed.WriteMessage(
                            $"\n  {Path.GetFileName(dwg)}: REFUSED -- result " +
                            $"measures worse ({before.OverlapArea:F1} -> " +
                            $"{shipped.OverlapArea:F1} mm2), not saved");
                        continue;
                    }
                    tr.Commit();
                    ed.WriteMessage(
                        $"\n  {Path.GetFileName(dwg)}: " +
                        $"{before.OverlapArea:F1} -> {shipped.OverlapArea:F1} mm2, " +
                        $"{scene.Labels.Count(l => l.Moved())} moved");
                }
                // ORIGINALS ARE NEVER OVERWRITTEN.
                db.SaveAs(Path.Combine(outDir, Path.GetFileName(dwg)),
                          DwgVersion.Current);
            }
            catch (System.Exception ex)
            {
                ed.WriteMessage($"\n  {Path.GetFileName(dwg)}: FAILED -- {ex.Message}");
            }
        }
        ed.WriteMessage($"\nDone.  Tidied copies in {outDir}\n");
    }
}
