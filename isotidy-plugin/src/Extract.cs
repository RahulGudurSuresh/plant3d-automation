// Database -> Scene: the dirty layer, and the file that replaces extract.py.
// Everything AutoCAD-specific on the read side lives here so Model/Detect/
// Solve/FreeSpace stay pure geometry.
//
// In-process advantages over the DXF prototype, worth knowing:
//   - MTEXT extents come from AutoCAD's OWN layout engine
//     (GeometricExtents), so the ezdxf wrap bug that once understated every
//     score simply cannot happen here.
//   - No ODA conversion, no round-trip risk: we read the same database the
//     engineer is looking at.

using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using AcPolyline = Autodesk.AutoCAD.DatabaseServices.Polyline;

namespace IsoTidy;

public static class Extract
{
    const double ArrowheadTol = 2.5;   // arrowhead centroid -> line end, mm
    const double BalloonLeaderTol = 3.0;
    const double OrphanLeaderTol = 10.0;

    public static Scene FromDatabase(Database db, Transaction tr,
                                     LayerRoles roles, Tuning cfg)
    {
        var scene = new Scene();
        var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
        var ms = (BlockTableRecord)tr.GetObject(
            bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);

        var textEnts = new List<Entity>();
        var dimEnts = new List<Dimension>();
        var balloonEnts = new List<BlockReference>();
        var mleaders = new List<MLeader>();
        var frameEnts = new List<Entity>();
        var priorLeaders = new List<Poly>();
        var curves = new List<List<Vec2>>();          // every drawn curve (ink)

        double sx0 = double.MaxValue, sy0 = double.MaxValue,
               sx1 = double.MinValue, sy1 = double.MinValue;
        void Grow(Extents3d e)
        {
            sx0 = Math.Min(sx0, e.MinPoint.X); sy0 = Math.Min(sy0, e.MinPoint.Y);
            sx1 = Math.Max(sx1, e.MaxPoint.X); sy1 = Math.Max(sy1, e.MaxPoint.Y);
        }

        // ---- first sweep: classify -------------------------------------
        foreach (ObjectId id in ms)
        {
            if (tr.GetObject(id, OpenMode.ForRead) is not Entity e) continue;
            try { Grow(e.GeometricExtents); } catch { }
            string layer = e.Layer;

            if (layer == roles.Leader)
            {
                // Our own previous output: not a label, but its owner must
                // remember the leader exists (see prior-leader claim below).
                if (e is Line pl)
                    priorLeaders.Add(new Poly(
                        [V(pl.StartPoint), V(pl.EndPoint)], cfg.GeomBuffer));
                continue;
            }
            switch (e)
            {
                case MText or DBText:
                    if (roles.IsMovable(layer)) textEnts.Add(e);
                    else if (!roles.Frame.Contains(layer))
                        scene.UnmappedText[layer] =
                            scene.UnmappedText.GetValueOrDefault(layer) + 1;
                    continue;
                case Dimension dim when roles.ReadDimensions
                                        && roles.IsMovable(layer):
                    dimEnts.Add(dim);
                    continue;
                case MLeader ml:
                    mleaders.Add(ml);
                    continue;
                case BlockReference br when !roles.Frame.Contains(layer)
                                            && HasAttributes(br, tr)
                                            && roles.IsBalloon(BlockName(br, tr)):
                    balloonEnts.Add(br);
                    continue;
            }
            if (roles.Frame.Contains(layer)) { frameEnts.Add(e); continue; }

            // Everything else is an obstacle -- symbols by their DRAWN
            // CURVES, never their box (a bbox walls off visually empty paper).
            var cs = Flatten(e, tr, 0);
            if (cs.Count > 0)
            {
                foreach (var c in cs)
                {
                    scene.Obstacles.Add(new Obstacle
                        { Path = new Poly(c.ToArray(), cfg.GeomBuffer) });
                    curves.Add(c);
                }
            }
            else if (TryBounds(e, out var box))
                scene.Obstacles.Add(new Obstacle { Box = box });
        }

        if (sx1 <= sx0) return scene;    // empty drawing
        var sheetExt = new Rect(sx0, sy0, sx1, sy1);
        scene.Sheet = sheetExt.Grow(-cfg.FrameMargin);
        foreach (var f in frameEnts)
            if (TryBounds(f, out var fb) && fb.Area < 0.4 * sheetExt.Area)
                scene.Reserved.Add(fb);

        // ---- leader index ----------------------------------------------
        var leaderPts = new List<(Vec2 landing, Vec2 tip, ObjectId id)>();
        foreach (var ml in mleaders)
        {
            if (!TryLeaderEnds(ml, out var tip, out var landing)) continue;
            leaderPts.Add((landing, tip, ml.ObjectId));
            // Someone else's leader is an obstacle; the owner handle lets
            // its own callout off the hook (Scene.OwnExempt).
            scene.Obstacles.Add(new Obstacle
            {
                Path = new Poly([tip, landing], cfg.GeomBuffer),
                Owner = ml.ObjectId,
            });
            curves.Add([tip, landing]);
        }

        // ---- labels: plain text ----------------------------------------
        foreach (var e in textEnts)
        {
            if (!TryBounds(e, out var b)) continue;
            var lab = new Label
            {
                Index = scene.Labels.Count, Id = e.ObjectId,
                Text = e is MText m ? m.Text : ((DBText)e).TextString,
                Cls = roles.LabelClass(e.Layer), Kind = "text",
                Pos = new Vec2(b.X0, b.Y0), Home = new Vec2(b.X0, b.Y0),
                Size = new Vec2(b.W, b.H),
            };
            lab.Anchor = NearestObstaclePoint(scene, lab.Center());
            scene.Labels.Add(lab);
        }

        // ---- labels: dimensions ----------------------------------------
        var dimPerp = new List<(Label lab, double perp)>();
        foreach (var dim in dimEnts)
        {
            var (textBox, line) = DimensionParts(dim, tr);
            if (textBox is not Rect tb) continue;
            var lab = new Label
            {
                Index = scene.Labels.Count, Id = dim.ObjectId,
                Text = dim.DimensionText is { Length: > 0 } t ? t : "<dim>",
                Cls = "dimension", Kind = "dimension",
                Pos = new Vec2(tb.X0, tb.Y0), Home = new Vec2(tb.X0, tb.Y0),
                Size = new Vec2(tb.W, tb.H),
            };
            if (line is { } dl)
            {
                var (a, b2) = dl;
                var mid = new Vec2((a.X + b2.X) / 2, (a.Y + b2.Y) / 2);
                var dir = b2 - a;
                double L = dir.Len();
                lab.Anchor = mid;
                lab.Slide = L > 1e-9 ? new Vec2(dir.X / L, dir.Y / L) : null;
                // The dimension OWNS its line -- other labels still collide
                // with it, its own text does not.
                scene.Obstacles.Add(new Obstacle
                    { Path = new Poly([a, b2], cfg.GeomBuffer), Owner = dim.ObjectId });
                if (lab.Slide is Vec2 s)
                {
                    var n = new Vec2(-s.Y, s.X);
                    dimPerp.Add((lab, Math.Abs((lab.Center() - mid).Dot(n))));
                }
            }
            else
                lab.Anchor = lab.Center();

            // EVERY line the dimension draws (witness lines, ticks) is an
            // owned obstacle -- on dense sheets they form most of the grid.
            foreach (var c in DimBlockCurves(dim, tr))
            {
                scene.Obstacles.Add(new Obstacle
                    { Path = new Poly(c.ToArray(), cfg.GeomBuffer), Owner = dim.ObjectId });
                curves.Add(c);
            }
            scene.Labels.Add(lab);
        }

        // THE SOLVER MAY NOT PLACE DIMENSION TEXT WHERE THE AUDIT CALLS IT
        // DRIFT: the limit is derived from the sheet's own median offset,
        // exactly as the audit derives it, minus a margin.
        if (dimPerp.Count > 0)
        {
            var med = dimPerp.Select(d => d.perp).OrderBy(p => p)
                             .ElementAt(dimPerp.Count / 2);
            double limit = Math.Max(Tuning.DimDriftFloor,
                                    Tuning.DimDriftFactor * med)
                           - Tuning.SlideLimitMargin;
            foreach (var (lab, _p) in dimPerp)
                lab.SlideLimit = limit;
        }

        // ---- labels: balloons ------------------------------------------
        foreach (var br in balloonEnts)
        {
            if (!TryBounds(br, out var b)) continue;
            var lab = new Label
            {
                Index = scene.Labels.Count, Id = br.ObjectId,
                Text = AttribText(br, tr), Cls = "balloon", Kind = "balloon",
                Pos = new Vec2(b.X0, b.Y0), Home = new Vec2(b.X0, b.Y0),
                Size = new Vec2(b.W, b.H),
            };
            var box = lab.Box();
            var pair = leaderPts
                .Select(l => (d: box.Distance(l.landing), l))
                .Where(x => x.d <= BalloonLeaderTol)
                .OrderBy(x => x.d).Select(x => x.l).FirstOrDefault();
            if (pair.id != ObjectId.Null)
            {
                lab.Anchor = pair.tip;          // arrow tip: the TRUE anchor
                lab.LeaderId = pair.id;
            }
            else
                lab.Anchor = NearestObstaclePoint(scene, lab.Center());
            scene.Labels.Add(lab);
        }

        GroupBalloonsWithText(scene, cfg);
        GroupCalloutStacks(scene, cfg);
        ClaimOrphanLeaders(scene, leaderPts);
        SplitMultiOwnerGroups(scene);

        // Prior-run LINE leaders: their label keeps them across runs.
        foreach (var g in priorLeaders)
        {
            Label owner = null; double best = 1.5;
            foreach (var lab in scene.Labels)
            {
                double d = Math.Min(lab.Box().Distance(g.Pts[0]),
                                    lab.Box().Distance(g.Pts[^1]));
                if (d < best) { best = d; owner = lab; }
            }
            if (owner != null) owner.DrawnLeader = true;
        }

        // ---- ink + free-space grid -------------------------------------
        foreach (var c in curves)
            scene.Ink.Add(new Poly(c.ToArray(), cfg.InkHalfwidth));
        try
        {
            var grid = new FreeSpace(sheetExt, cfg.FreeCell);
            foreach (var c in curves) grid.AddSegments(c, cfg.InkHalfwidth);
            foreach (var o in scene.Obstacles)
                if (o.Path == null && o.Box.W < 400 && o.Box.H < 400)
                    grid.AddRect(o.Box);
            // The title block is not free space: stamp what the frame draws.
            foreach (var f in frameEnts)
            {
                foreach (var c in Flatten(f, tr, 0)) grid.AddSegments(c, cfg.GeomBuffer);
                if (TryBounds(f, out var fb) && fb.Area < 0.4 * sheetExt.Area)
                    grid.AddRect(fb);
            }
            scene.Free = grid.Finish();
        }
        catch { scene.Free = null; }   // a missing map must never break a solve

        return scene;
    }

    // -------------------------------------------------------------------
    static Vec2 V(Point3d p) => new(p.X, p.Y);

    static bool TryBounds(Entity e, out Rect r)
    {
        try
        {
            var x = e.GeometricExtents;
            r = new Rect(x.MinPoint.X, x.MinPoint.Y, x.MaxPoint.X, x.MaxPoint.Y);
            return r.W > 0 || r.H > 0;
        }
        catch { r = default; return false; }
    }

    static bool HasAttributes(BlockReference br, Transaction tr)
    {
        foreach (ObjectId _ in br.AttributeCollection) return true;
        return false;
    }

    static string BlockName(BlockReference br, Transaction tr)
    {
        try
        {
            var btr = (BlockTableRecord)tr.GetObject(
                br.DynamicBlockTableRecord, OpenMode.ForRead);
            return btr.Name;
        }
        catch { return br.Name; }
    }

    static string AttribText(BlockReference br, Transaction tr)
    {
        var parts = new List<string>();
        foreach (ObjectId aid in br.AttributeCollection)
            if (tr.GetObject(aid, OpenMode.ForRead) is AttributeReference ar)
                parts.Add(ar.TextString);
        return string.Join("/", parts);
    }

    /// An entity's drawn curves as point chains, block references exploded.
    static List<List<Vec2>> Flatten(Entity e, Transaction tr, int depth)
    {
        var outp = new List<List<Vec2>>();
        try
        {
            switch (e)
            {
                case Line l:
                    outp.Add([V(l.StartPoint), V(l.EndPoint)]);
                    break;
                case AcPolyline pl:
                    var pts = new List<Vec2>();
                    for (int i = 0; i < pl.NumberOfVertices; i++)
                        pts.Add(new Vec2(pl.GetPoint2dAt(i).X, pl.GetPoint2dAt(i).Y));
                    if (pl.Closed && pts.Count > 0) pts.Add(pts[0]);
                    if (pts.Count >= 2) outp.Add(pts);
                    break;
                case Curve c and (Circle or Arc or Ellipse or Spline):
                    outp.Add(SampleCurve(c));
                    break;
                case Solid s:
                    outp.Add([V(s.GetPointAt(0)), V(s.GetPointAt(1)),
                              V(s.GetPointAt(2))]);
                    break;
                case BlockReference br when depth < 4:
                    var col = new DBObjectCollection();
                    br.Explode(col);
                    foreach (DBObject o in col)
                    {
                        if (o is Entity sub)
                            outp.AddRange(Flatten(sub, tr, depth + 1));
                        o.Dispose();
                    }
                    break;
            }
        }
        catch { }
        return outp.Where(c => c.Count >= 2).ToList();
    }

    static List<Vec2> SampleCurve(Curve c)
    {
        var pts = new List<Vec2>();
        try
        {
            double s0 = c.StartParam, s1 = c.EndParam;
            const int N = 24;
            for (int i = 0; i <= N; i++)
            {
                var p = c.GetPointAtParameter(s0 + (s1 - s0) * i / N);
                pts.Add(V(p));
            }
        }
        catch { }
        return pts;
    }

    /// (text box, dimension line) from the dimension's anonymous *D block.
    /// THE DIMENSION LINE IS THE ONE CARRYING THE ARROWHEADS, NOT THE
    /// LONGEST -- witness lines outgrow the dimension line on short dims.
    static (Rect?, (Vec2, Vec2)?) DimensionParts(Dimension dim, Transaction tr)
    {
        Rect? text = null;
        var lines = new List<(Vec2 a, Vec2 b)>();
        var arrows = new List<Vec2>();
        try
        {
            var blk = (BlockTableRecord)tr.GetObject(
                dim.DimBlockId, OpenMode.ForRead);
            foreach (ObjectId id in blk)
            {
                var e = tr.GetObject(id, OpenMode.ForRead);
                switch (e)
                {
                    case MText or DBText:
                        if (TryBounds((Entity)e, out var tb))
                            text = text is Rect prev
                                ? new Rect(Math.Min(prev.X0, tb.X0),
                                           Math.Min(prev.Y0, tb.Y0),
                                           Math.Max(prev.X1, tb.X1),
                                           Math.Max(prev.Y1, tb.Y1))
                                : tb;
                        break;
                    case Line l:
                        lines.Add((V(l.StartPoint), V(l.EndPoint)));
                        break;
                    case Solid s:
                        arrows.Add(new Vec2(
                            (s.GetPointAt(0).X + s.GetPointAt(1).X + s.GetPointAt(2).X) / 3,
                            (s.GetPointAt(0).Y + s.GetPointAt(1).Y + s.GetPointAt(2).Y) / 3));
                        break;
                }
            }
        }
        catch { }
        if (text is null || lines.Count == 0) return (text, null);

        int Support((Vec2 a, Vec2 b) l) => arrows.Count(c =>
            (c - l.a).Len() <= ArrowheadTol || (c - l.b).Len() <= ArrowheadTol);
        var bestLine = lines
            .OrderByDescending(Support)
            .ThenByDescending(l => (l.b - l.a).Len())
            .First();
        return (text, bestLine);
    }

    static IEnumerable<List<Vec2>> DimBlockCurves(Dimension dim, Transaction tr)
    {
        var outp = new List<List<Vec2>>();
        try
        {
            var blk = (BlockTableRecord)tr.GetObject(
                dim.DimBlockId, OpenMode.ForRead);
            foreach (ObjectId id in blk)
                if (tr.GetObject(id, OpenMode.ForRead) is Entity e
                    and not (MText or DBText))
                    outp.AddRange(Flatten(e, tr, 1));
        }
        catch { }
        return outp;
    }

    static bool TryLeaderEnds(MLeader ml, out Vec2 tip, out Vec2 landing)
    {
        tip = landing = default;
        try
        {
            // Leader-line index 0: ISOGEN emits one line per MULTILEADER.
            // If your release exposes a different vertex API surface, this
            // is the ONE method to adapt (see README, "API notes").
            tip = V(ml.GetFirstVertex(0));
            landing = V(ml.GetLastVertex(0));
            return (tip - landing).Len() > 1e-6;
        }
        catch { return false; }
    }

    static Vec2 NearestObstaclePoint(Scene scene, Vec2 from)
    {
        // Fallback anchor for plain text: nearest point on fixed geometry.
        // A guess, and treated as one -- leaders to guessed anchors are only
        // drawn when a label has TRAVELLED (writeback threshold).
        Vec2 best = from; double bestD = double.MaxValue;
        foreach (var o in scene.Obstacles)
        {
            if (!o.Owner.IsNull) continue;
            double d = o.Distance(from);
            if (d < bestD)
            {
                bestD = d;
                best = o.Path != null
                    ? NearestOnPoly(o.Path, from)
                    : o.Box.Nearest(from);
            }
        }
        return best;
    }

    static Vec2 NearestOnPoly(Poly p, Vec2 from)
    {
        Vec2 best = p.Pts[0]; double bestD = double.MaxValue;
        for (int i = 0; i + 1 < p.Pts.Length; i++)
        {
            var a = p.Pts[i]; var b = p.Pts[i + 1];
            var ab = b - a;
            double L2 = ab.Dot(ab);
            double t = L2 < 1e-12 ? 0 : Math.Clamp((from - a).Dot(ab) / L2, 0, 1);
            var q = new Vec2(a.X + ab.X * t, a.Y + ab.Y * t);
            double d = (from - q).Len();
            if (d < bestD) { bestD = d; best = q; }
        }
        return best;
    }

    /// Weld each item balloon to the size text it is tucked onto (ISOGEN
    /// convention -- treating the tuck as a defect is the mistake that once
    /// produced 309 mm2 of phantom overlap).
    static void GroupBalloonsWithText(Scene scene, Tuning cfg)
    {
        var balloons = scene.Labels.Where(l => l.Cls == "balloon").ToList();
        var texts = scene.Labels.Where(l => l.Cls == "annotation").ToList();
        var claims = new List<(double score, int bi, int ti)>();
        foreach (var b in balloons)
            foreach (var t in texts)
            {
                double inter = b.Box().IntersectionArea(t.Box());
                double score;
                if (inter > 0) score = inter;
                else
                {
                    double gap = b.Box().Distance(t.Box());
                    if (gap > cfg.PairGap) continue;
                    score = -gap;
                }
                claims.Add((score, b.Index, t.Index));
            }
        claims.Sort((x, y) => y.score != x.score
            ? y.score.CompareTo(x.score)
            : (x.bi != y.bi ? x.bi.CompareTo(y.bi) : x.ti.CompareTo(y.ti)));
        var takenB = new HashSet<int>(); var takenT = new HashSet<int>();
        int next = 0;
        foreach (var (_, bi, ti) in claims)
        {
            if (!takenB.Add(bi) || !takenT.Add(ti)) continue;
            scene.Labels[bi].Group = next;
            scene.Labels[ti].Group = next;
            next++;
        }
    }

    /// Weld ISOGEN's stacked callouts (left-aligned column, uniform gap,
    /// one leader on the top member) into one rigid group.
    static void GroupCalloutStacks(Scene scene, Tuning cfg)
    {
        var idx = scene.Labels
            .Where(l => l.Cls is "annotation" or "balloon")
            .Select(l => l.Index).ToArray();
        if (idx.Length < 2) return;

        var parent = Enumerable.Range(0, scene.Labels.Count).ToArray();
        int Find(int i) { while (parent[i] != i) i = parent[i] = parent[parent[i]]; return i; }
        void Union(int a, int b) { int ra = Find(a), rb = Find(b); if (ra != rb) parent[rb] = ra; }

        foreach (var g in scene.Labels.Where(l => l.Group is not null)
                                      .GroupBy(l => l.Group))
        {
            var m = g.Select(l => l.Index).ToArray();
            for (int i = 1; i < m.Length; i++) Union(m[0], m[i]);
        }
        foreach (var a in idx)
            foreach (var b in idx)
            {
                if (b <= a) continue;
                var la = scene.Labels[a]; var lb = scene.Labels[b];
                if (Math.Abs(la.Pos.X - lb.Pos.X) > cfg.StackAlign) continue;
                double gap = Math.Max(
                    lb.Pos.Y - (la.Pos.Y + la.Size.Y),
                    la.Pos.Y - (lb.Pos.Y + lb.Size.Y));
                if (gap >= -0.5 && gap <= cfg.StackGap) Union(a, b);
            }

        var comps = new Dictionary<int, List<int>>();
        for (int i = 0; i < scene.Labels.Count; i++)
        {
            int r = Find(i);
            if (!comps.TryGetValue(r, out var list)) comps[r] = list = new();
            list.Add(i);
        }
        int nextG = 0;
        foreach (var members in comps.Values.Where(m => m.Count >= 2))
        {
            foreach (var m in members) scene.Labels[m].Group = nextG;
            nextG++;
        }
    }

    /// A weld may contain at most ONE leader (twin of
    /// extract._split_multi_owner_groups).  The tuck/stack rules weld by
    /// geometry alone; a balloon sitting ON a note, each with its own arrow,
    /// got welded and their mutual overlap vanished from every score.  Each
    /// distinct leader seeds its own group; leaderless members follow the
    /// nearest owner so a genuine stack keeps its single arrow.
    static void SplitMultiOwnerGroups(Scene scene)
    {
        var groups = scene.Labels.Where(l => l.Group is not null)
                                 .GroupBy(l => l.Group!.Value)
                                 .ToDictionary(g => g.Key, g => g.ToList());
        if (groups.Count == 0) return;
        int next = groups.Keys.Max() + 1;
        foreach (var (g, mem) in groups)
        {
            var owners = mem.Where(l => !l.LeaderId.IsNull)
                            .GroupBy(l => l.LeaderId).ToList();
            if (owners.Count < 2) continue;
            var seeds = new List<Label>();
            int k = 0;
            foreach (var grp in owners)
            {
                int gid = k == 0 ? g : next++;
                foreach (var l in grp) l.Group = gid;
                seeds.Add(grp.First());
                k++;
            }
            foreach (var l in mem)
            {
                if (!l.LeaderId.IsNull) continue;
                var near = seeds.OrderBy(s => l.Box().Distance(s.Box())).First();
                l.Group = near.Group;
            }
        }
    }

    static void ClaimOrphanLeaders(Scene scene,
        List<(Vec2 landing, Vec2 tip, ObjectId id)> leaders)
    {
        var claimed = scene.Labels.Where(l => !l.LeaderId.IsNull)
                                  .Select(l => l.LeaderId).ToHashSet();
        var claims = new List<(double d, int seq, int labIdx)>();
        var orphans = leaders.Where(l => !claimed.Contains(l.id)).ToList();
        for (int oi = 0; oi < orphans.Count; oi++)
            foreach (var lab in scene.Labels)
            {
                if (!lab.LeaderId.IsNull) continue;
                double d = lab.Box().Distance(orphans[oi].landing);
                if (d <= OrphanLeaderTol) claims.Add((d, oi, lab.Index));
            }
        claims.Sort();
        var takenO = new HashSet<int>();
        foreach (var (_, oi, li) in claims)
        {
            if (takenO.Contains(oi) || !scene.Labels[li].LeaderId.IsNull) continue;
            takenO.Add(oi);
            scene.Labels[li].LeaderId = orphans[oi].id;
            scene.Labels[li].Anchor = orphans[oi].tip;
        }
    }
}
