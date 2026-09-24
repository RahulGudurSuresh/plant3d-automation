// Scene -> Database: the mirror of Extract, and the file that replaces
// writeback.py.  The tool never rebuilds an annotation, it only MOVES it --
// style, height, layer, XDATA all survive, which is what makes the output
// acceptable to a checker.
//
// Everything happens inside the caller's single transaction, which AutoCAD
// wraps in ONE undo step per command: the engineer presses Ctrl+Z once and
// the whole tidy-up is rejected.

using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;

namespace IsoTidy;

public static class Writeback
{
    const int LeaderColor = 8;   // grey: a leader is chrome, not content

    public static (int moved, int leaders) Apply(
        Database db, Transaction tr, Scene scene, LayerRoles roles, Tuning cfg)
    {
        EnsureLayer(db, tr, roles.Leader);
        ClearOldLeaders(db, tr, roles.Leader);

        // ONE leader per welded callout, whoever draws it.
        var groupHasMl = scene.Labels
            .Where(l => l.Group is not null && !l.LeaderId.IsNull)
            .Select(l => l.Group!.Value).ToHashSet();
        var drawnGroups = new HashSet<int>();
        int moved = 0, leaders = 0;

        bool NeedsLine(Label lab) =>
            lab.LeaderId.IsNull
            && (lab.Group is not int g
                || (!groupHasMl.Contains(g) && !drawnGroups.Contains(g)));

        void LineFor(Label lab)
        {
            if (DrawLeader(db, tr, scene, lab, roles.Leader, cfg))
            {
                leaders++;
                lab.DrawnLeader = true;
                if (lab.Group is int g) drawnGroups.Add(g);
            }
        }

        foreach (var lab in scene.Labels)
        {
            if (!lab.Moved())
            {
                // A leader WE drew in an earlier run was just cleared; the
                // label did not move this time (home already the far spot),
                // so redraw it where the label stands or the association is
                // silently lost.
                if (lab.DrawnLeader && NeedsLine(lab)) LineFor(lab);
                continue;
            }
            var d = lab.Delta();
            bool ok = lab.Kind switch
            {
                "dimension" => MoveDimension(tr, lab, d),
                "balloon" => MoveBalloon(tr, lab, d, scene),
                _ => MoveText(tr, lab, d, scene),
            };
            if (!ok) continue;
            moved++;
            // Anything moved far without a MULTILEADER needs a line, or an
            // overlap was traded for an ambiguity -- a QA defect on an iso.
            // NEVER for dimension text: it only slides along its own
            // dimension line, whose witness lines already tie it to the
            // part.  (The reference writeback.py has no dimension branch
            // here; the port applied the rule to every kind and hung a
            // leader on seven dimension texts of JP1070-028LL.)
            if (lab.Kind != "dimension"
                && NeedsLine(lab)
                && (lab.Displacement() >= cfg.LeaderThreshold || lab.DrawnLeader))
                LineFor(lab);
        }
        return (moved, leaders);
    }

    // -------------------------------------------------------------------
    static bool MoveText(Transaction tr, Label lab, Vec2 d, Scene scene)
    {
        if (tr.GetObject(lab.Id, OpenMode.ForWrite) is not Entity e) return false;
        e.TransformBy(Matrix3d.Displacement(new Vector3d(d.X, d.Y, 0)));
        if (!lab.LeaderId.IsNull && !RelandLeader(tr, lab, scene))
        {
            e.TransformBy(Matrix3d.Displacement(new Vector3d(-d.X, -d.Y, 0)));
            return false;
        }
        return true;
    }

    static bool MoveDimension(Transaction tr, Label lab, Vec2 d)
    {
        if (tr.GetObject(lab.Id, OpenMode.ForWrite) is not Dimension dim)
            return false;
        try
        {
            // ISOGEN's dimension style says DIMTMOVE=1: "add a leader when
            // the text is moved".  When AutoCAD regenerates the dimension
            // after our move it obeys that and draws a stub from the
            // dimension line to the relocated number -- the small magenta
            // "legend lines" reported on 2026-09-11.  Override per
            // dimension to 2 = move the text freely, no leader; the text
            // stays on (or within the drift bar of) its own line anyway.
            try { dim.Dimtmove = 2; } catch { }
            var tp = dim.TextPosition;
            dim.TextPosition = new Point3d(tp.X + d.X, tp.Y + d.Y, tp.Z);
            dim.UsingDefaultTextPosition = false;
            // In-process, AutoCAD regenerates the *D block itself -- the
            // cached-MTEXT double-write the DXF path needed does not exist.
            dim.RecomputeDimensionBlock(true);
            return true;
        }
        catch { return false; }
    }

    static bool MoveBalloon(Transaction tr, Label lab, Vec2 d, Scene scene)
    {
        if (tr.GetObject(lab.Id, OpenMode.ForWrite) is not BlockReference br)
            return false;
        TranslateBlock(tr, br, d);
        if (!RelandLeader(tr, lab, scene))
        {
            TranslateBlock(tr, br, new Vec2(-d.X, -d.Y));
            return false;   // a leader pointing nowhere is worse than no move
        }
        return true;
    }

    /// Move a block reference AND its attribute text by exactly `d`, once.
    ///
    /// 2026-09-08, first live run on JP1070-028LL: the previous code called
    /// TransformBy on the reference and then AGAIN on every attribute, on the
    /// belief that attributes do not follow the reference.  AutoCAD's
    /// BlockReference.TransformBy DOES carry its attributes, so every moved
    /// balloon number and tag text landed at TWICE the displacement while
    /// its circle/rectangle moved once -- empty circles with stray digits
    /// beside them, tag frames separated from their text.  The detector saw
    /// none of it (it scores the scene, not the entities), so the command
    /// reported a 99% improvement on a wrecked sheet.
    ///
    /// This version does not trust either belief: it records where the
    /// attributes are, transforms the reference, and only transforms an
    /// attribute itself if the reference did not already carry it.
    internal static void TranslateBlock(Transaction tr, BlockReference br, Vec2 d)
    {
        var m = Matrix3d.Displacement(new Vector3d(d.X, d.Y, 0));
        var before = new Dictionary<ObjectId, Point3d>();
        foreach (ObjectId aid in br.AttributeCollection)
            if (tr.GetObject(aid, OpenMode.ForRead) is AttributeReference ar)
                before[aid] = ar.Position;
        br.TransformBy(m);
        foreach (var (aid, p0) in before)
        {
            if (tr.GetObject(aid, OpenMode.ForWrite) is not AttributeReference ar)
                continue;
            if (ar.Position.DistanceTo(p0) < 1e-6)   // did not follow: move it
                ar.TransformBy(m);
        }
    }

    /// Bring the label's MULTILEADER landing along; the ARROW TIP NEVER
    /// MOVES (hard rule from the design engineer).  The landing is chosen
    /// crossing-checked: nearest point plus the four edge midpoints, scored
    /// by (labels crossed, geometry crossed, length).
    static bool RelandLeader(Transaction tr, Label lab, Scene scene)
    {
        if (lab.LeaderId.IsNull) return true;
        if (tr.GetObject(lab.LeaderId, OpenMode.ForWrite) is not MLeader ml)
            return true;
        try
        {
            var best = PickLanding(scene, lab);
            ml.SetLastVertex(0, new Point3d(best.X, best.Y, 0));
            return true;
        }
        catch { return false; }
    }

    /// Where a leader from the anchor should meet its label: nearest point
    /// plus the four edge midpoints, scored by (labels crossed, geometry
    /// crossed, length) -- one policy for re-landed MULTILEADERs and for the
    /// LINE leaders we draw, exactly as writeback.py shares _pick_landing.
    static Vec2 PickLanding(Scene scene, Label lab)
    {
        var tip = lab.Anchor;
        var box = lab.Box();
        var near = box.Nearest(tip);
        Vec2[] cands =
        [
            near,
            new((box.X0 + box.X1) / 2, box.Y0),
            new((box.X0 + box.X1) / 2, box.Y1),
            new(box.X0, (box.Y0 + box.Y1) / 2),
            new(box.X1, (box.Y0 + box.Y1) / 2),
        ];
        var exempt = scene.OwnExempt(lab);
        var ownGroup = lab.Group is int g
            ? scene.Labels.Where(l => l.Group == g).Select(l => l.Index).ToHashSet()
            : new HashSet<int> { lab.Index };

        (int lx, int gx, double len) Score(Vec2 cand)
        {
            int labX = scene.Labels.Count(o =>
                !ownGroup.Contains(o.Index)
                && Geo.SegLenInRect(tip, cand, o.Box()) > 1e-9);
            int geoX = scene.ObstaclesNear(
                    new Rect(Math.Min(tip.X, cand.X), Math.Min(tip.Y, cand.Y),
                             Math.Max(tip.X, cand.X), Math.Max(tip.Y, cand.Y)).Grow(1),
                    exempt)
                .Count(o => o.Distance(tip) > 0.05 && o.CrossesSeg(tip, cand));
            return (labX, geoX, (cand - tip).Len());
        }

        return cands.Select((c, i) => (s: Score(c), i, c))
                    .OrderBy(x => x.s.lx).ThenBy(x => x.s.gx)
                    .ThenBy(x => x.s.len).ThenBy(x => x.i)
                    .First().c;
    }

    static bool DrawLeader(Database db, Transaction tr, Scene scene,
                           Label lab, string layer, Tuning cfg)
    {
        if (lab.Box(cfg.Clearance * 0.5).Contains(lab.Anchor)) return false;
        // Same crossing-checked landing policy as a re-landed MULTILEADER
        // (writeback.py _pick_landing): fewest labels crossed, then fewest
        // geometry crossings, then shortest.  Nearest-point alone drew
        // hairlines straight through neighbouring text on JP1070-028LL.
        var landing = PickLanding(scene, lab);
        var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
        var ms = (BlockTableRecord)tr.GetObject(
            bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite);
        var line = new Line(new Point3d(lab.Anchor.X, lab.Anchor.Y, 0),
                            new Point3d(landing.X, landing.Y, 0))
        { Layer = layer };
        ms.AppendEntity(line);
        tr.AddNewlyCreatedDBObject(line, true);
        return true;
    }

    static void EnsureLayer(Database db, Transaction tr, string name)
    {
        var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
        if (lt.Has(name)) return;
        lt.UpgradeOpen();
        var rec = new LayerTableRecord
        {
            Name = name,
            Color = Autodesk.AutoCAD.Colors.Color.FromColorIndex(
                Autodesk.AutoCAD.Colors.ColorMethod.ByAci, (short)LeaderColor),
        };
        lt.Add(rec);
        tr.AddNewlyCreatedDBObject(rec, true);
    }

    /// Delete leaders from a previous run so re-running is idempotent.
    /// (Ownership is restored right after: Extract marked the labels whose
    /// leaders these were via DrawnLeader, and Apply redraws them.)
    static void ClearOldLeaders(Database db, Transaction tr, string layer)
    {
        var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
        var ms = (BlockTableRecord)tr.GetObject(
            bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
        foreach (ObjectId id in ms)
            if (tr.GetObject(id, OpenMode.ForRead) is Line l && l.Layer == layer)
            {
                l.UpgradeOpen();
                l.Erase();
            }
    }
}
