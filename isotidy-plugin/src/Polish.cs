// The 2026-09 defect classes, in-process.  Everything here was learned on
// the Python side against 71 real sheets (CONTEXT.md 12b-12d) and ported:
//
//   1  RESERVED TABLES   the DESIGN DATA table lives inside the title-block
//      INSERT, which is bigger than the 40% sheet cut-off in Extract, so
//      nothing ever priced it -- the Python solver parked dim text on it.
//      The table rect is measured from the title block's own rule lines
//      and added to Scene.Reserved BEFORE solving.
//   2  DOGLEG HYGIENE    ISOGEN leaves vestigial doglegs on CONNECTED
//      notes (47 of them fleet-wide): a horizontal stub running through
//      the note's own text, or pointing away from it, or being the
//      leader's ONLY attachment.  Strip + re-land on the note's near edge.
//      A dogleg pointing INTO the text start is ISOGEN's proper pattern
//      and is kept.  NOTE: the managed MLeader API keeps the entity- and
//      context-level dogleg fields in sync automatically -- the
//      half-updated-record anomaly the DXF path shipped (CONTEXT 12d)
//      cannot happen here.
//   3  OWN-RUN           a leader landing on the far edge of its own
//      label runs through the text it belongs to.  >2 mm inside the own
//      box -> re-land on the near edge.
//   4  FRAMES            ISOGEN's AnnoRect* blocks draw one fixed-width
//      rectangle whatever the tag length.  In-process we measure the TRUE
//      rendered text width (real fonts -- no substitution gap), so:
//      rect must be text + 2 mm margins, text centred in the rect,
//      welded stacks keep the rect's left edge on the column line.
//
// Arrow tips are never moved.  Every reland is crossing-checked at the
// same bar the writeback uses, plus the two aesthetic rules from 202M01:
// leaders 1.5 mm apart minimum (except twins fanning from one shared
// arrowhead), and no landing that threads a text gap.

using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;

namespace IsoTidy;

public static class Polish
{
    const double OwnRunTol = 2.0;    // mm through the own box = crossing
    const double Sep = 1.5;          // closeness reads as crossing
    const double Hub = 3.0;          // shared-arrowhead exemption radius
    const double FrameMargin = 2.0;  // air per side inside a widened rect
    const double Comfort = 2.5;      // a text's comfort zone ...
    const double ThreadMax = 8.0;    // ... a leader may run inside it this far
    const double ClaimTol = 8.0;     // orphan leader -> stack union distance
    const double RelocateReach = 60; // how far a stack may be moved

    // ---- leader geometry, read once per stage -----------------------------
    sealed class Ld
    {
        public ObjectId Id; public Vec2 Tip, Land; public MLeader Ml;
    }

    static List<Ld> Leaders(Transaction tr, Database db)
    {
        var outp = new List<Ld>();
        var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
        var ms = (BlockTableRecord)tr.GetObject(
            bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
        foreach (ObjectId id in ms)
        {
            if (tr.GetObject(id, OpenMode.ForRead) is not MLeader ml) continue;
            try
            {
                var t = ml.GetFirstVertex(0); var l = ml.GetLastVertex(0);
                outp.Add(new Ld { Id = id, Ml = ml,
                                  Tip = new Vec2(t.X, t.Y),
                                  Land = new Vec2(l.X, l.Y) });
            }
            catch { }
        }
        return outp;
    }

    static double Cross(Vec2 o, Vec2 a, Vec2 b) =>
        (a.X - o.X) * (b.Y - o.Y) - (a.Y - o.Y) * (b.X - o.X);

    static bool SegCross(Vec2 a, Vec2 b, Vec2 c, Vec2 d)
    {
        double d1 = Cross(c, d, a), d2 = Cross(c, d, b);
        double d3 = Cross(a, b, c), d4 = Cross(a, b, d);
        return ((d1 > 0) != (d2 > 0)) && ((d3 > 0) != (d4 > 0));
    }

    static double PointSegDist(Vec2 p, Vec2 a, Vec2 b)
    {
        var ab = b - a; double L2 = ab.Dot(ab);
        double t = L2 < 1e-12 ? 0 : Math.Clamp((p - a).Dot(ab) / L2, 0, 1);
        return (p - new Vec2(a.X + ab.X * t, a.Y + ab.Y * t)).Len();
    }

    /// Minimum separation between two leaders, judged OUTSIDE every arrow
    /// hub (arrows converging on one part are inherently close there).
    static double SegSep(Vec2 a, Vec2 b, Vec2 c, Vec2 d, List<Vec2> tips)
    {
        double best = double.MaxValue;
        const int N = 24;
        for (int i = 0; i <= N; i++)
        {
            var p = new Vec2(a.X + (b.X - a.X) * i / N, a.Y + (b.Y - a.Y) * i / N);
            if (tips.Any(t => (t - p).Len() <= Hub)) continue;
            best = Math.Min(best, PointSegDist(p, c, d));
            var q = new Vec2(c.X + (d.X - c.X) * i / N, c.Y + (d.Y - c.Y) * i / N);
            if (tips.Any(t => (t - q).Len() <= Hub)) continue;
            best = Math.Min(best, PointSegDist(q, a, b));
        }
        return best;
    }

    /// THE shared leader rule set (mirrors rework.Ctx.line_clean): no
    /// crossing another leader; >= 1.5 mm from every other leader outside
    /// the hubs, except twins fanning from one shared arrowhead; never
    /// inside another text's hard pad; never > 8 mm inside its 2.5 mm
    /// comfort zone (threading); may touch its own boxes, never run
    /// through them; no component symbol crossed.
    static bool LeaderClean(Scene scene, Tuning cfg, Vec2 tip, Vec2 land,
                            ObjectId self, HashSet<int> ownIdx,
                            IEnumerable<Rect> ownBoxes, List<Ld> lds)
    {
        var tips = lds.Select(l => l.Tip).ToList();
        foreach (var o in lds)
        {
            if (o.Id == self) continue;
            if (SegCross(tip, land, o.Tip, o.Land)) return false;
            bool twins = (o.Tip - tip).Len() < 0.5;
            if (!twins && SegSep(tip, land, o.Tip, o.Land, tips) < Sep)
                return false;
        }
        double pad = cfg.Clearance + cfg.GeomBuffer;
        foreach (var o in scene.Labels)
        {
            if (ownIdx.Contains(o.Index)) continue;
            if (Geo.SegLenInRect(tip, land, o.Box(pad)) > 1e-9) return false;
            if (Geo.SegLenInRect(tip, land, o.Box(Comfort)) > ThreadMax)
                return false;
        }
        foreach (var b in ownBoxes)
            if (Geo.SegLenInRect(tip, land, b.Grow(-0.05)) > OwnRunTol)
                return false;
        var span = new Rect(Math.Min(tip.X, land.X), Math.Min(tip.Y, land.Y),
                            Math.Max(tip.X, land.X), Math.Max(tip.Y, land.Y));
        var exempt = new HashSet<ObjectId> { self };
        foreach (var o in scene.ObstaclesNear(span.Grow(Sep), exempt))
        {
            if (!o.Owner.IsNull) continue;          // dims/leaders: own rules
            if (o.Distance(tip) <= Hub) continue;   // pointing, not crossing
            if (o.CrossesSeg(tip, land)) return false;
        }
        return true;
    }

    static HashSet<int> GroupIdx(Scene scene, Label lab) =>
        lab.Group is int g
            ? scene.Labels.Where(l => l.Group == g).Select(l => l.Index).ToHashSet()
            : new HashSet<int> { lab.Index };

    static IEnumerable<Vec2> EdgeCands(Rect b, Vec2 tip) =>
        new[]
        {
            new Vec2(b.X0, (b.Y0 + b.Y1) / 2), new Vec2(b.X1, (b.Y0 + b.Y1) / 2),
            new Vec2((b.X0 + b.X1) / 2, b.Y0), new Vec2((b.X0 + b.X1) / 2, b.Y1),
            new Vec2(b.X0, b.Y0), new Vec2(b.X1, b.Y0),
            new Vec2(b.X0, b.Y1), new Vec2(b.X1, b.Y1),
        }.OrderBy(c => (c - tip).Len());

    static void SetLanding(MLeader ml, Vec2 land)
    {
        try { ml.EnableDogleg = false; ml.DoglegLength = 0.0; } catch { }
        ml.SetLastVertex(0, new Point3d(land.X, land.Y, 0));
    }

    static void RefreshLeaderObstacle(Scene scene, Tuning cfg, Ld ld, Vec2 land)
    {
        foreach (var o in scene.Obstacles)
            if (o.Owner == ld.Id && o.Path != null)
                o.Path = new Poly([ld.Tip, land], cfg.GeomBuffer);
        ld.Land = land;
    }

    // ---- REALIGN: frames that drifted off their stack's column --------------
    /// Symmetric frame widening left tags 3-6 mm off the column their
    /// OPERATOR text and balloon sit on; the welder (0.3 mm) then saw two
    /// things instead of one stack and the tag's leader stopped speaking
    /// for the text (12 phantom C findings on 6 sheets, 2026-09-08).  Shift
    /// the FRAME -- rect, attribute text and its leader landing together --
    /// back onto the column; the texts stay where ISOGEN put them.
    public static int Realign(Transaction tr, Scene scene, Database db, Tuning cfg)
    {
        var lds = Leaders(tr, db);
        int n = 0;
        double pad = cfg.Clearance + cfg.GeomBuffer;
        var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
        var ms = (BlockTableRecord)tr.GetObject(
            bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
        foreach (ObjectId id in ms)
        {
            if (tr.GetObject(id, OpenMode.ForRead) is not BlockReference br) continue;
            if (!(br.Name ?? "").StartsWith("AnnoRect")) continue;
            var lab = scene.Labels.FirstOrDefault(l => l.Id == br.ObjectId);
            if (lab == null) continue;
            var fb = lab.Box();
            var cands = new List<(double adx, double dx, Label o)>();
            foreach (var o in scene.Labels)
            {
                if (o.Index == lab.Index || o.Kind == "dimension")
                    continue;
                var ob = o.Box();
                double vgap = Math.Max(ob.Y0 - fb.Y1, fb.Y0 - ob.Y1);
                if (vgap < -0.5 || vgap > cfg.StackGap) continue;
                if (ob.X1 < fb.X0 - 2 || ob.X0 > fb.X1 + 2) continue;
                double dx = ob.X0 - fb.X0;
                if (Math.Abs(dx) > cfg.StackAlign && Math.Abs(dx) <= 12.0)
                    cands.Add((Math.Abs(dx), dx, o));
            }
            if (cands.Count == 0) continue;
            // the re-welded stack must end with exactly ONE leader, whichever
            // member holds it; two distinct leaders = two callouts, not a stack
            var owners = cands.Select(c => c.o.LeaderId).Append(lab.LeaderId)
                              .Where(x => !x.IsNull).Distinct().Count();
            if (owners != 1) continue;
            var best = cands.OrderBy(c => c.adx).First();
            var mates = cands.Select(c => c.o.Index).ToHashSet();
            mates.Add(lab.Index);
            var moved = new Rect(fb.X0 + best.dx, fb.Y0, fb.X1 + best.dx, fb.Y1).Grow(pad);
            bool blocked = scene.OutsideAllowed(moved) > 1e-9
                || scene.Labels.Any(o => !mates.Contains(o.Index)
                                         && moved.IntersectionArea(o.Box(cfg.Clearance)) > 1e-9)
                || scene.ObstaclesNear(moved, scene.OwnExempt(lab)).Any(o => o.AreaOver(moved) > 1e-9);
            if (blocked) continue;
            var d = new Vec2(best.dx, 0);
            var brw = (BlockReference)tr.GetObject(br.ObjectId, OpenMode.ForWrite);
            Writeback.TranslateBlock(tr, brw, d);
            lab.Pos = lab.Pos + d; lab.Home = lab.Home + d;
            // a leader ON the frame rides along (same rect edge); one on a
            // mate stays put
            var ld = lab.LeaderId.IsNull ? null
                : lds.FirstOrDefault(x => x.Id == lab.LeaderId);
            if (ld != null)
            {
                var mlw = (MLeader)tr.GetObject(ld.Id, OpenMode.ForWrite);
                var land = new Vec2(ld.Land.X + d.X, ld.Land.Y);
                mlw.SetLastVertex(0, new Point3d(land.X, land.Y, 0));
                RefreshLeaderObstacle(scene, cfg, ld, land);
            }
            n++;
        }
        return n;
    }

    // ---- CLAIMS: orphan leaders aimed at a leaderless stack -----------------
    /// ISOGEN can land a stack's leader on the corner of the stack's OVERALL
    /// extent -- a point no member touches -- so no label claims it and the
    /// audit calls the stack a weak association (JP1070-028LL: 11.4 mm from
    /// the nearest member, 0 mm from the union).  Re-land on the near edge
    /// of the frame member and record the ownership.
    public static int Claims(Transaction tr, Scene scene, Database db,
                             Tuning cfg)
    {
        var lds = Leaders(tr, db);
        var owned = scene.Labels.Where(l => !l.LeaderId.IsNull)
                                .Select(l => l.LeaderId).ToHashSet();
        int n = 0;
        foreach (var ld in lds)
        {
            if (owned.Contains(ld.Id)) continue;
            Label best = null; double bestD = double.MaxValue;
            foreach (var l in scene.Labels)
            {
                if (!l.LeaderId.IsNull || l.Kind == "dimension") continue;
                var mates = l.Group is int g
                    ? scene.Labels.Where(m => m.Group == g).ToList()
                    : new List<Label> { l };
                if (mates.Any(m => !m.LeaderId.IsNull)) continue;
                var ub = new Rect(mates.Min(m => m.Box().X0), mates.Min(m => m.Box().Y0),
                                  mates.Max(m => m.Box().X1), mates.Max(m => m.Box().Y1));
                double d = ub.Distance(ld.Land);
                if (d <= ClaimTol && d < bestD) { bestD = d; best = l; }
            }
            if (best == null)
            {
                // Orphan whose dogleg points at NOTHING (469M01_r0-3: 41.7 mm,
                // nearest text 22 mm away): a phantom line.  Strip the
                // dogleg, keep the leader -- never worse.  A dogleg ending
                // within 3 mm of some text is an attachment: left alone.
                try
                {
                    if (ld.Ml.EnableDogleg && ld.Ml.DoglegLength > 5.0)
                    {
                        var v = ld.Ml.GetDogleg(0);
                        var end = new Vec2(ld.Land.X + v.X * ld.Ml.DoglegLength,
                                           ld.Land.Y + v.Y * ld.Ml.DoglegLength);
                        if (scene.Labels.All(l => l.Box().Distance(end) > 3.0))
                        {
                            var mlw = (MLeader)tr.GetObject(ld.Id, OpenMode.ForWrite);
                            mlw.EnableDogleg = false; mlw.DoglegLength = 0.0;
                            n++;
                        }
                    }
                }
                catch { }
                continue;
            }
            var target = best;
            if (best.Group is int gg)
            {
                var frame = scene.Labels.FirstOrDefault(m =>
                    m.Group == gg &&
                    tr.GetObject(m.Id, OpenMode.ForRead) is BlockReference br &&
                    (br.Name ?? "").StartsWith("AnnoRect"));
                if (frame != null) target = frame;
            }
            var ownIdx = GroupIdx(scene, target);
            var ownBoxes = scene.Labels.Where(l => ownIdx.Contains(l.Index))
                                       .Select(l => l.Box()).ToList();
            foreach (var c in EdgeCands(target.Box(), ld.Tip))
            {
                if (!LeaderClean(scene, cfg, ld.Tip, c, ld.Id, ownIdx, ownBoxes, lds))
                    continue;
                var ml = (MLeader)tr.GetObject(ld.Id, OpenMode.ForWrite);
                SetLanding(ml, c);
                RefreshLeaderObstacle(scene, cfg, ld, c);
                target.LeaderId = ld.Id;
                owned.Add(ld.Id);
                n++;
                break;
            }
        }
        return n;
    }

    // ---- RELOCATE: move a whole stack to the nearest clean slot ------------
    /// Optionally widening its frame member to `need` there.  Only stacks
    /// that own a leader: a relocated stack without one trades the defect
    /// for a weak association.  Runs AFTER Writeback.Apply, so Pos and Home
    /// are both advanced (Delta stays zero -- nothing moves twice).
    static bool RelocateStack(Transaction tr, Scene scene, Database db,
                              Tuning cfg, Label lab, List<Ld> lds,
                              BlockReference frame = null, double need = 0,
                              Rect? frameRect = null)
    {
        if (scene.Free == null) return false;
        var ownIdx = GroupIdx(scene, lab);
        var members = scene.Labels.Where(l => ownIdx.Contains(l.Index)).ToList();
        var leaderId = members.Select(l => l.LeaderId).FirstOrDefault(id => !id.IsNull);
        var ld = lds.FirstOrDefault(x => x.Id == leaderId);
        if (ld == null) return false;

        var boxes = new Dictionary<int, Rect>();
        foreach (var m in members)
        {
            var b = m.Box();
            if (frame != null && m.Index == lab.Index && frameRect is Rect fr)
            {
                bool stacked = members.Any(o => o.Index != lab.Index &&
                                                Math.Abs(o.Box().X0 - fr.X0) < 0.8);
                double x0 = stacked ? fr.X0 : (fr.X0 + fr.X1) / 2 - need / 2;
                b = new Rect(x0, fr.Y0, x0 + need, fr.Y1);
            }
            boxes[m.Index] = b;
        }
        double gx0 = boxes.Values.Min(b => b.X0), gy0 = boxes.Values.Min(b => b.Y0);
        var size = new Vec2(boxes.Values.Max(b => b.X1) - gx0,
                            boxes.Values.Max(b => b.Y1) - gy0);
        double pad = cfg.Clearance + cfg.GeomBuffer;
        var exempt = scene.OwnExempt(lab);

        bool BoxOk(Rect b)
        {
            var g = b.Grow(pad);
            if (scene.OutsideAllowed(g) > 1e-9) return false;
            foreach (var o in scene.Labels)
                if (!ownIdx.Contains(o.Index) &&
                    g.IntersectionArea(o.Box(cfg.Clearance)) > 1e-9) return false;
            foreach (var o in scene.ObstaclesNear(g, exempt))
                if (o.AreaOver(g) > 1e-9) return false;
            if (scene.InkUnder(g) > 1e-9) return false;
            foreach (var o in lds)
                if (o.Id != ld.Id && Geo.SegLenInRect(o.Tip, o.Land, g) > 1e-9)
                    return false;
            return true;
        }

        (double score, Vec2 d, Vec2 land)? best = null;
        foreach (var slot in scene.Free.OpenSlots(new Vec2(gx0, gy0), size, pad,
                                                  RelocateReach, 2.0, 200, 1.5))
        {
            var d = new Vec2(slot.X - gx0, slot.Y - gy0);
            var cand = boxes.ToDictionary(kv => kv.Key,
                kv => new Rect(kv.Value.X0 + d.X, kv.Value.Y0 + d.Y,
                               kv.Value.X1 + d.X, kv.Value.Y1 + d.Y));
            if (!cand.Values.All(BoxOk)) continue;
            var landBox = cand[lab.Index];
            foreach (var c in EdgeCands(landBox, ld.Tip))
            {
                if (!LeaderClean(scene, cfg, ld.Tip, c, ld.Id, ownIdx, cand.Values, lds))
                    continue;
                double score = (c - ld.Tip).Len() + 2 * d.Len();
                if (best is null || score < best.Value.score) best = (score, d, c);
                break;
            }
        }
        if (best is null) return false;
        var (_, dd, landing) = best.Value;

        foreach (var m in members)
        {
            var ent = tr.GetObject(m.Id, OpenMode.ForWrite) as Entity;
            if (ent is BlockReference br) Writeback.TranslateBlock(tr, br, dd);
            else ent?.TransformBy(Matrix3d.Displacement(new Vector3d(dd.X, dd.Y, 0)));
            m.Pos = m.Pos + dd; m.Home = m.Home + dd;
        }
        if (frame != null && frameRect is Rect fr2)
        {
            var brw = (BlockReference)tr.GetObject(frame.ObjectId, OpenMode.ForWrite);
            WidenFrame(tr, brw, need, boxes[lab.Index].X0 + dd.X, fr2.W);
            lab.Size = new Vec2(need, lab.Size.Y);
            lab.Pos = new Vec2(boxes[lab.Index].X0 + dd.X, lab.Pos.Y);
            lab.Home = lab.Pos;
        }
        var mlw = (MLeader)tr.GetObject(ld.Id, OpenMode.ForWrite);
        SetLanding(mlw, landing);
        RefreshLeaderObstacle(scene, cfg, ld, landing);
        return true;
    }

    /// Scale the AnnoRect block to `need` mm wide, pin its left edge at
    /// `newLeft`, and re-centre the attribute text in it.  The attribute
    /// text keeps its own size; only the rectangle grows.
    static void WidenFrame(Transaction tr, BlockReference br, double need,
                           double newLeft, double rectW)
    {
        var btr = (BlockTableRecord)tr.GetObject(br.BlockTableRecord, OpenMode.ForRead);
        double bx0 = double.MaxValue;
        foreach (ObjectId eid in btr)
            if (tr.GetObject(eid, OpenMode.ForRead) is Curve cv)
                try { bx0 = Math.Min(bx0, cv.GeometricExtents.MinPoint.X); } catch { }
        double sx = br.ScaleFactors.X;
        br.ScaleFactors = new Scale3d(sx * need / rectW, br.ScaleFactors.Y, br.ScaleFactors.Z);
        double leftNow = br.Position.X + bx0 * sx * need / rectW;
        br.Position = new Point3d(br.Position.X + (newLeft - leftNow),
                                  br.Position.Y, br.Position.Z);
        double tx0 = double.MaxValue, tx1 = double.MinValue;
        var ars = new List<AttributeReference>();
        foreach (ObjectId aid in br.AttributeCollection)
            if (tr.GetObject(aid, OpenMode.ForWrite) is AttributeReference ar)
            {
                ars.Add(ar);
                try
                {
                    var ae = ar.GeometricExtents;
                    tx0 = Math.Min(tx0, ae.MinPoint.X); tx1 = Math.Max(tx1, ae.MaxPoint.X);
                }
                catch { }
            }
        if (tx1 <= tx0) return;
        double off = (tx0 + tx1) / 2 - (newLeft + need / 2);
        foreach (var ar in ars)
            ar.TransformBy(Matrix3d.Displacement(new Vector3d(-off, 0, 0)));
    }

    // ---- THREADS: leaders that thread, graze, or crowd -------------------
    /// Re-check every owned leader against the shared rule set on its
    /// CURRENT path; a violator is re-landed on another edge of its label,
    /// and if no edge is clean the stack is relocated.
    public static int Threads(Transaction tr, Scene scene, Database db, Tuning cfg)
    {
        var lds = Leaders(tr, db);
        int n = 0;
        foreach (var lab in scene.Labels)
        {
            if (lab.LeaderId.IsNull) continue;
            var ld = lds.FirstOrDefault(x => x.Id == lab.LeaderId);
            if (ld == null) continue;
            var ownIdx = GroupIdx(scene, lab);
            var ownBoxes = scene.Labels.Where(l => ownIdx.Contains(l.Index))
                                       .Select(l => l.Box()).ToList();
            if (LeaderClean(scene, cfg, ld.Tip, ld.Land, ld.Id, ownIdx, ownBoxes, lds))
                continue;
            bool done = false;
            foreach (var c in EdgeCands(lab.Box(), ld.Tip))
            {
                if (!LeaderClean(scene, cfg, ld.Tip, c, ld.Id, ownIdx, ownBoxes, lds))
                    continue;
                var ml = (MLeader)tr.GetObject(ld.Id, OpenMode.ForWrite);
                SetLanding(ml, c);
                RefreshLeaderObstacle(scene, cfg, ld, c);
                done = true;
                break;
            }
            if (!done) done = RelocateStack(tr, scene, db, cfg, lab, lds);
            if (done) n++;
        }
        return n;
    }

    // ---- HAIRLINES: residual grazes the solver could not slide clear ------
    public static int Hairlines(Transaction tr, Scene scene, Database db, Tuning cfg)
    {
        var lds = Leaders(tr, db);
        var rep = Detector.Detect(scene, cfg);
        var seen = new HashSet<int>();
        int n = 0;
        foreach (var c in rep.Collisions)
        {
            if (c.Kind != "geometry" || c.Area > 5.0) continue;
            var lab = scene.Labels[c.A];
            if (lab.Kind == "dimension") continue;       // slide-pinned
            int key = lab.Group ?? -1 - lab.Index;
            if (!seen.Add(key)) continue;
            if (RelocateStack(tr, scene, db, cfg, lab, lds)) n++;
        }
        return n;
    }

    // ---- 1. reserve the title-block tables --------------------------------
    /// The DESIGN DATA table's rect, measured from the title block's own
    /// horizontal/vertical rule lines (never hardcoded: sheet templates
    /// differ).  Returns how many rects were reserved.
    public static int ReserveTitleBlockTables(Transaction tr, Scene scene,
                                              Database db)
    {
        var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
        var ms = (BlockTableRecord)tr.GetObject(
            bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
        foreach (ObjectId id in ms)
        {
            if (tr.GetObject(id, OpenMode.ForRead) is not BlockReference br)
                continue;
            Extents3d ext;
            try { ext = br.GeometricExtents; } catch { continue; }
            if (ext.MaxPoint.X - ext.MinPoint.X < 700) continue;  // not it

            var btr = (BlockTableRecord)tr.GetObject(
                br.BlockTableRecord, OpenMode.ForRead);
            var xf = br.BlockTransform;
            var ys = new SortedSet<double>();
            var xs = new SortedSet<double>();
            foreach (ObjectId eid in btr)
            {
                if (tr.GetObject(eid, OpenMode.ForRead) is not Line ln)
                    continue;
                var a = ln.StartPoint.TransformBy(xf);
                var b = ln.EndPoint.TransformBy(xf);
                if (Math.Max(a.Y, b.Y) > 170 || Math.Max(a.X, b.X) > 340)
                    continue;                       // outside the table zone
                if (Math.Abs(a.Y - b.Y) < 0.5 && Math.Abs(a.X - b.X) > 50)
                    ys.Add(Math.Round(a.Y, 1));
                if (Math.Abs(a.X - b.X) < 0.5 && Math.Abs(a.Y - b.Y) > 20)
                    xs.Add(Math.Round(a.X, 1));
            }
            if (ys.Count < 4 || xs.Count < 2) return 0;
            scene.Reserved.Add(new Rect(xs.Min, ys.Min, xs.Max, ys.Max));
            return 1;
        }
        return 0;
    }

    // ---- shared reland machinery ------------------------------------------
    static bool LineClean(Scene scene, Label own, Vec2 tip, Vec2 land,
                          Tuning cfg)
    {
        var ownGroup = own.Group is int g
            ? scene.Labels.Where(l => l.Group == g)
                          .Select(l => l.Index).ToHashSet()
            : new HashSet<int> { own.Index };
        foreach (var o in scene.Labels)
        {
            if (ownGroup.Contains(o.Index)) continue;
            if (Geo.SegLenInRect(tip, land,
                    o.Box(cfg.Clearance + cfg.GeomBuffer)) > 1e-9)
                return false;
        }
        // own box: may TOUCH the edge, must not run through the interior
        if (Geo.SegLenInRect(tip, land, own.Box(-0.05)) > OwnRunTol)
            return false;
        var exempt = scene.OwnExempt(own);
        var span = new Rect(Math.Min(tip.X, land.X), Math.Min(tip.Y, land.Y),
                            Math.Max(tip.X, land.X), Math.Max(tip.Y, land.Y));
        foreach (var o in scene.ObstaclesNear(span.Grow(Sep), exempt))
        {
            if (o.Distance(tip) <= Hub) continue;   // pointing, not crossing
            if (o.CrossesSeg(tip, land)) return false;
        }
        return true;
    }

    static bool RelandNearEdge(Transaction tr, Scene scene, Label lab,
                               MLeader ml, Vec2 tip, Tuning cfg)
    {
        var b = lab.Box();
        Vec2[] cands =
        [
            new(b.X0, (b.Y0 + b.Y1) / 2), new(b.X1, (b.Y0 + b.Y1) / 2),
            new((b.X0 + b.X1) / 2, b.Y0), new((b.X0 + b.X1) / 2, b.Y1),
            new(b.X0, b.Y0), new(b.X1, b.Y0), new(b.X0, b.Y1),
            new(b.X1, b.Y1),
        ];
        foreach (var c in cands.OrderBy(c => (c - tip).Len()))
        {
            if (!LineClean(scene, lab, tip, c, cfg)) continue;
            try
            {
                ml.EnableDogleg = false;   // API syncs entity AND context
                ml.DoglegLength = 0.0;
            }
            catch { /* some releases refuse when already off */ }
            ml.SetLastVertex(0, new Point3d(c.X, c.Y, 0));
            return true;
        }
        return false;
    }

    // ---- 2 + 3. dogleg hygiene and own-run landings -----------------------
    public static (int doglegs, int ownRuns) LeaderHygiene(
        Transaction tr, Scene scene, Tuning cfg)
    {
        int doglegs = 0, ownRuns = 0;
        foreach (var lab in scene.Labels)
        {
            if (lab.LeaderId.IsNull) continue;
            if (tr.GetObject(lab.LeaderId, OpenMode.ForWrite) is not MLeader ml)
                continue;
            Vec2 tip, land;
            try
            {
                var t = ml.GetFirstVertex(0);
                var l = ml.GetLastVertex(0);
                tip = new Vec2(t.X, t.Y);
                land = new Vec2(l.X, l.Y);
            }
            catch { continue; }

            double dl = 0;
            bool hasDog = false;
            Vec2? dogEnd = null;
            try
            {
                hasDog = ml.EnableDogleg;
                dl = ml.DoglegLength;
                if (hasDog && dl > 0.1)
                {
                    var v = ml.GetDogleg(0);      // per-release API; guarded
                    dogEnd = new Vec2(land.X + v.X * dl, land.Y + v.Y * dl);
                }
            }
            catch { dogEnd = null; }

            var box = lab.Box();
            double runPlain = Geo.SegLenInRect(tip, land, box.Grow(-0.05));
            double runDog = dogEnd is Vec2 de
                ? Geo.SegLenInRect(land, de, box.Grow(-0.05)) : 0;
            bool pointsAway = dogEnd is Vec2 d2
                && box.Distance(d2) > box.Distance(land) + 0.5;

            if (hasDog && dl > 0.1 && (runDog > 0.5 || pointsAway
                                       || runPlain > OwnRunTol))
            {
                if (RelandNearEdge(tr, scene, lab, ml, tip, cfg)) doglegs++;
                else if (pointsAway)
                {
                    // no clean edge to re-land on, but a dogleg pointing
                    // AWAY from its own label is a phantom line whatever
                    // else is true (166M05_r0-3): strip it, keep the landing
                    try { ml.EnableDogleg = false; ml.DoglegLength = 0.0; doglegs++; }
                    catch { }
                }
            }
            else if (runPlain > OwnRunTol)
            {
                if (RelandNearEdge(tr, scene, lab, ml, tip, cfg)) ownRuns++;
            }
        }
        return (doglegs, ownRuns);
    }

    // ---- 4. frames --------------------------------------------------------
    public static int FixFrames(Transaction tr, Scene scene, Database db,
                                Tuning cfg)
    {
        int fixedN = 0;
        var lds = Leaders(tr, db);
        var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
        var ms = (BlockTableRecord)tr.GetObject(
            bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
        foreach (ObjectId id in ms)
        {
            if (tr.GetObject(id, OpenMode.ForRead) is not BlockReference br)
                continue;
            var name = br.Name ?? "";
            if (!name.StartsWith("AnnoRect")) continue;
            if (Math.Abs(br.Rotation) > 1e-6) continue;   // never seen rotated
            if (br.AttributeCollection.Count == 0) continue;

            // rect: the block's own curves, in world units
            var btr = (BlockTableRecord)tr.GetObject(
                br.BlockTableRecord, OpenMode.ForRead);
            double bx0 = double.MaxValue, bx1 = double.MinValue;
            double by0 = double.MaxValue, by1 = double.MinValue;
            foreach (ObjectId eid in btr)
            {
                if (tr.GetObject(eid, OpenMode.ForRead) is not Curve cv)
                    continue;
                try
                {
                    var ce = cv.GeometricExtents;
                    bx0 = Math.Min(bx0, ce.MinPoint.X);
                    bx1 = Math.Max(bx1, ce.MaxPoint.X);
                    by0 = Math.Min(by0, ce.MinPoint.Y);
                    by1 = Math.Max(by1, ce.MaxPoint.Y);
                }
                catch { }
            }
            if (bx1 <= bx0) continue;
            double sx = br.ScaleFactors.X, sy = br.ScaleFactors.Y;
            double rectW = (bx1 - bx0) * sx;
            double rectLeft = br.Position.X + bx0 * sx;
            var rectWorld = new Rect(rectLeft, br.Position.Y + by0 * sy,
                                     rectLeft + rectW, br.Position.Y + by1 * sy);

            // TRUE rendered width -- in-process the real fonts measure it
            double tx0 = double.MaxValue, tx1 = double.MinValue, tcy = 0;
            AttributeReference first = null;
            foreach (ObjectId aid in br.AttributeCollection)
            {
                if (tr.GetObject(aid, OpenMode.ForRead)
                        is not AttributeReference ar) continue;
                first ??= ar;
                try
                {
                    var ae = ar.GeometricExtents;
                    tx0 = Math.Min(tx0, ae.MinPoint.X);
                    tx1 = Math.Max(tx1, ae.MaxPoint.X);
                    tcy = (ae.MinPoint.Y + ae.MaxPoint.Y) / 2;
                }
                catch { }
            }
            if (tx1 <= tx0 || first == null) continue;
            double textW = tx1 - tx0;
            double need = textW + 2 * FrameMargin;
            if (rectW >= need - 0.2)
            {
                // wide enough: just make sure the text is centred
                double off = (tx0 + tx1) / 2 - (rectLeft + rectW / 2);
                if (Math.Abs(off) > 0.5)
                {
                    var arw = (AttributeReference)tr.GetObject(
                        first.ObjectId, OpenMode.ForWrite);
                    arw.TransformBy(Matrix3d.Displacement(
                        new Vector3d(-off, 0, 0)));
                    fixedN++;
                }
                continue;
            }

            // welded stack keeps its column line; free tags grow centred,
            // then right-only, then left-only -- first clean strip wins
            var lab = scene.Labels.FirstOrDefault(l => l.Id == br.ObjectId);
            double grow = need - rectW;
            var lefts = new List<double>();
            bool stacked = false;
            if (lab != null && lab.Group is int g)
                foreach (var m in scene.Labels)
                    if (m.Group == g && m.Index != lab.Index
                        && Math.Abs(m.Box().X0 - rectLeft) < 0.8)
                    { stacked = true; break; }
            if (stacked) lefts.Add(rectLeft);
            else
            {
                lefts.Add(rectLeft - grow / 2);
                lefts.Add(rectLeft);
                lefts.Add(rectLeft - grow);
            }
            bool grown = false;
            foreach (var newLeft in lefts)
            {
                var strips = new List<Rect>();
                if (newLeft < rectLeft - 1e-9)
                    strips.Add(new Rect(newLeft, tcy - 4, rectLeft, tcy + 4));
                if (newLeft + need > rectLeft + rectW + 1e-9)
                    strips.Add(new Rect(rectLeft + rectW, tcy - 4,
                                        newLeft + need, tcy + 4));
                bool ok = true;
                foreach (var s in strips)
                {
                    if (scene.OutsideAllowed(s) > 1e-9) ok = false;
                    foreach (var o in scene.Labels)
                    {
                        if (lab != null && (o.Index == lab.Index ||
                                (lab.Group is int gg && o.Group == gg)))
                            continue;
                        if (s.IntersectionArea(o.Box(cfg.Clearance)) > 1e-9)
                            ok = false;
                    }
                    foreach (var o in scene.ObstaclesNear(s,
                                 lab != null ? scene.OwnExempt(lab) : null))
                        if (o.AreaOver(s) > 1e-9 || o.CrossesSeg(
                                new Vec2(s.X0, s.Y0), new Vec2(s.X1, s.Y1)))
                            ok = false;
                }
                if (!ok) continue;

                var brw = (BlockReference)tr.GetObject(
                    br.ObjectId, OpenMode.ForWrite);
                brw.ScaleFactors = new Scale3d(
                    sx * need / rectW, br.ScaleFactors.Y,
                    br.ScaleFactors.Z);
                double leftNow = brw.Position.X + bx0 * sx * need / rectW;
                brw.Position = new Point3d(
                    brw.Position.X + (newLeft - leftNow),
                    brw.Position.Y, brw.Position.Z);
                // text stays its own size; re-centre it in the new rect
                double cx = newLeft + need / 2;
                double offNow = (tx0 + tx1) / 2 - cx;
                foreach (ObjectId aid in brw.AttributeCollection)
                    if (tr.GetObject(aid, OpenMode.ForWrite)
                            is AttributeReference arw)
                        arw.TransformBy(Matrix3d.Displacement(
                            new Vector3d(-offNow, 0, 0)));
                fixedN++;
                grown = true;
                break;
            }
            // Every growth direction collides (470M01, JP1070-028LL):
            // move the whole stack to a clean slot and widen it there.
            if (!grown && lab != null &&
                RelocateStack(tr, scene, db, cfg, lab, lds, br, need, rectWorld))
                fixedN++;
        }
        return fixedN;
    }
}
