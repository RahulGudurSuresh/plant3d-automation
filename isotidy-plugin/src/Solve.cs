// Label placement -- the C# twin of isotidy/solve.py.
//
// Greedy + hill-climb over a candidate menu, fully deterministic (same
// drawing in, same drawing out -- non-determinism is why drafters abandon
// automation).  Two vetoes stand above the cost blend:
//
//   INK VETO       wherever a callout moves, the drawn ink beneath it must
//                  not exceed what ISOGEN left it on at HOME (+0.5 mm2
//                  slack).  A veto, not a weight: it cannot be outbid.
//   SCOREKEEPER    after every accepted move the independent detector
//                  re-measures the WHOLE sheet; total overlap up => revert.
//
// Deliberately not ported yet (the Python reference still owns them, see
// README "scope"): _shave_tucks (cosmetic tuck relief) and the category-B
// leader re-router (tools/reroute.py).

namespace IsoTidy;

public sealed class SolveStats
{
    public int Passes, Moved;
}

sealed class Unit
{
    public int[] Members;
    public Vec2[] Offsets;          // each member's home relative to unit home
    public Vec2 Home, Size, Anchor;
    public Vec2? Slide;
    public double? SlideLimit;
    public int Lead;                // index INTO Members of the leader-owner
    public double HomeAnchorDist;

    public Vec2[] Positions(Vec2 pos) =>
        Offsets.Select(o => pos + o).ToArray();
}

public static class Solver
{
    public static SolveStats Solve(Scene scene, Tuning cfg)
    {
        var stats = new SolveStats();
        if (scene.Labels.Count == 0) return stats;

        var units = BuildUnits(scene);
        var boxes = scene.Labels.Select(l => l.Box(cfg.Clearance)).ToArray();

        for (int pass = 0; pass < cfg.MaxPasses; pass++)
        {
            stats.Passes = pass + 1;
            var inTrouble = units
                .Where(u => UnitTrouble(scene, cfg, u, UnitPos(scene, u), boxes) > 1e-9)
                .OrderByDescending(u => UnitCost(scene, cfg, u, UnitPos(scene, u), boxes))
                .ThenBy(u => u.Members[0])
                .ToList();

            double gain = 0;
            foreach (var unit in inTrouble)
            {
                var at = UnitPos(scene, unit);
                double here = UnitCost(scene, cfg, unit, at, boxes);
                double budget = InkOf(scene, unit, unit.Home) + cfg.InkTolerance;
                Vec2 bestPos = at; double bestCost = here;
                foreach (var cand in Candidates(scene, cfg, unit))
                {
                    if (InkOf(scene, unit, cand) > budget) continue;   // INK VETO
                    double c = UnitCost(scene, cfg, unit, cand, boxes);
                    if (c < bestCost - 1e-9) { bestCost = c; bestPos = cand; }
                }
                if ((bestPos - at).Len() > 1e-9)
                {
                    // SCOREKEEPER: the referee re-measures; worse => revert.
                    double before = Detector.Detect(scene, cfg).OverlapArea;
                    Place(scene, cfg, unit, bestPos, boxes);
                    if (Detector.Detect(scene, cfg).OverlapArea > before + 1e-9)
                        Place(scene, cfg, unit, at, boxes);
                    else
                        gain += here - bestCost;
                }
            }
            if (gain < 1e-6) break;
        }

        // ZERO-RESIDUAL SWEEP: a graze "not worth" the travel cost survives
        // hill-climbing; the requirement is not cheap moves, it is NO
        // overlap.  Nearest clean candidate wins, ink-vetoed as always,
        // kept only if the measured overlap strictly drops.
        foreach (var unit in units)
        {
            var at = UnitPos(scene, unit);
            if (UnitTrouble(scene, cfg, unit, at, boxes) <= 1e-9) continue;
            double budget = InkOf(scene, unit, unit.Home) + cfg.InkTolerance;
            var clean = Candidates(scene, cfg, unit)
                .Where(c => UnitTrouble(scene, cfg, unit, c, boxes) <= 1e-9
                            && InkOf(scene, unit, c) <= budget)
                .Select(c => (d: Math.Round((c - at).Len(), 6), c))
                .OrderBy(x => x.d).ThenBy(x => x.c.X).ThenBy(x => x.c.Y)
                .Take(5);
            foreach (var (_, cand) in clean)
            {
                double before = Detector.Detect(scene, cfg).OverlapArea;
                Place(scene, cfg, unit, cand, boxes);
                if (Detector.Detect(scene, cfg).OverlapArea < before - 1e-9) break;
                Place(scene, cfg, unit, at, boxes);
            }
        }

        stats.Moved = scene.Labels.Count(l => l.Moved());
        return stats;
    }

    // -------------------------------------------------------------------
    static List<Unit> BuildUnits(Scene scene)
    {
        var units = new List<Unit>();
        foreach (var members in scene.RigidUnits())
        {
            var labs = members.Select(i => scene.Labels[i]).ToArray();
            double x1 = labs.Min(l => l.Home.X), y1 = labs.Min(l => l.Home.Y);
            double x2 = labs.Max(l => l.Home.X + l.Size.X);
            double y2 = labs.Max(l => l.Home.Y + l.Size.Y);
            int lead = Array.FindIndex(labs, l => !l.LeaderId.IsNull);
            if (lead < 0) lead = 0;
            units.Add(new Unit
            {
                Members = members,
                Offsets = labs.Select(l => l.Home - new Vec2(x1, y1)).ToArray(),
                Home = new Vec2(x1, y1),
                Size = new Vec2(x2 - x1, y2 - y1),
                Anchor = labs[lead].Anchor,
                Slide = labs.Length == 1 ? labs[lead].Slide : null,
                SlideLimit = labs.Length == 1 ? labs[lead].SlideLimit : null,
                Lead = lead,
                HomeAnchorDist = labs[lead].Box(0, labs[lead].Home)
                                           .Distance(labs[lead].Anchor),
            });
        }
        return units;
    }

    static Vec2 UnitPos(Scene scene, Unit u) =>
        scene.Labels[u.Members[0]].Pos - u.Offsets[0];

    static void Place(Scene scene, Tuning cfg, Unit u, Vec2 pos, Rect[] boxes)
    {
        var ps = u.Positions(pos);
        for (int k = 0; k < u.Members.Length; k++)
        {
            scene.Labels[u.Members[k]].Pos = ps[k];
            boxes[u.Members[k]] = scene.Labels[u.Members[k]].Box(cfg.Clearance);
        }
    }

    static double InkOf(Scene scene, Unit u, Vec2 pos)
    {
        double total = 0;
        var ps = u.Positions(pos);
        for (int k = 0; k < u.Members.Length; k++)
            total += scene.InkUnder(scene.Labels[u.Members[k]].Box(0, ps[k]));
        return total;
    }

    static IEnumerable<Vec2> Candidates(Scene scene, Tuning cfg, Unit u)
    {
        yield return u.Home;      // home first: ties resolve to "don't move"
        double w = u.Size.X, h = u.Size.Y;

        if (u.Slide is Vec2 s)
        {
            // CONSTRAINED: dimension text slides along its line, may flip
            // across it -- and NEVER past the audit's drift bar (SlideLimit).
            var n = new Vec2(-s.Y, s.X);
            var homeC = new Vec2(u.Home.X + w / 2, u.Home.Y + h / 2);
            double h0 = (homeC - u.Anchor).Dot(n);
            double gap = Math.Abs(n.X) * w + Math.Abs(n.Y) * h + 0.8;   // 2026-09-17: one clearance past the line, not two
            double[] offsets = [0, 1.2, -1.2, 2.4, -2.4, 3.6, -3.6,
                                gap, -gap, 2 * gap, -2 * gap];
            foreach (var step in cfg.SlideSteps)
                foreach (var k in offsets)
                {
                    if (step == 0 && k == 0) continue;
                    if (u.SlideLimit is double lim && Math.Abs(h0 + k) > lim)
                        continue;
                    yield return new Vec2(u.Home.X + s.X * step + n.X * k,
                                          u.Home.Y + s.Y * step + n.Y * k);
                }
            yield break;
        }

        // FREE: rings around home (escapes stay local) AND around the
        // anchor (hop to the far side of the part), 30-degree steps so a
        // moved label sits parallel to the pipe run it belongs to.
        var hc = new Vec2(u.Home.X + w / 2, u.Home.Y + h / 2);
        for (int k = 0; k < cfg.NDirections; k++)
        {
            double th = 2 * Math.PI * k / cfg.NDirections;
            double ux = Math.Cos(th), uy = Math.Sin(th);
            foreach (var r in cfg.Radii)
                yield return new Vec2(hc.X + ux * r - w / 2, hc.Y + uy * r - h / 2);
        }
        for (int k = 0; k < cfg.NDirections; k++)
        {
            double th = 2 * Math.PI * k / cfg.NDirections;
            double ux = Math.Cos(th), uy = Math.Sin(th);
            foreach (var r in cfg.Radii)
                yield return new Vec2(u.Anchor.X + ux * (r + w / 2) - w / 2,
                                      u.Anchor.Y + uy * (r + h / 2) - h / 2);
        }
        // Gaps the rings cannot reach: ask the sheet where the box fits.
        if (scene.Free != null)
            foreach (var slot in scene.Free.OpenSlots(
                         u.Home, u.Size, cfg.Clearance,
                         cfg.FreeReach, cfg.FreeStep, cfg.FreeSlots, cfg.FreeHalo))
                yield return slot;
    }

    static int LeaderCrossings(Scene scene, Tuning cfg, Label lead, Rect box,
                               Rect[] boxes, HashSet<int> memberSet,
                               HashSet<Autodesk.AutoCAD.DatabaseServices.ObjectId> exempt)
    {
        var tip = lead.Anchor;
        if (box.Contains(tip)) return 0;
        var landing = box.Nearest(tip);
        int n = 0;
        foreach (var o in scene.ObstaclesNear(
                     new Rect(Math.Min(tip.X, landing.X), Math.Min(tip.Y, landing.Y),
                              Math.Max(tip.X, landing.X), Math.Max(tip.Y, landing.Y)).Grow(1),
                     exempt))
            if (o.Distance(tip) > 0.05 && o.CrossesSeg(tip, landing))
                n++;
        for (int j = 0; j < boxes.Length; j++)
        {
            if (j == lead.Index || memberSet.Contains(j)) continue;
            if (Geo.SegLenInRect(tip, landing, boxes[j]) > 1e-9) n++;
        }
        return n;
    }

    static double UnitTrouble(Scene scene, Tuning cfg, Unit u, Vec2 pos, Rect[] boxes)
    {
        var memberSet = new HashSet<int>(u.Members);
        var lead = scene.Labels[u.Members[u.Lead]];
        var exempt = scene.OwnExempt(lead);
        double c = 0;
        var ps = u.Positions(pos);

        for (int k = 0; k < u.Members.Length; k++)
        {
            var lab = scene.Labels[u.Members[k]];
            var b = lab.Box(cfg.Clearance, ps[k]);
            var raw = lab.Box(0, ps[k]);
            for (int j = 0; j < boxes.Length; j++)
            {
                if (memberSet.Contains(j)) continue;
                double inter = b.IntersectionArea(boxes[j]);
                if (inter > 0) c += cfg.WLabel * inter;
                // NEAR-MISSES COST TOO: a label a hair beyond the clearance
                // pad scores zero yet reads as touching.  Extent-aware:
                // grow both raw boxes by half the band, price the overlap.
                var other = scene.Labels[j].Box();
                if (raw.Distance(other) < cfg.ProximityBand)
                {
                    double half = cfg.ProximityBand / 2;
                    c += cfg.WProximity *
                         raw.Grow(half).IntersectionArea(other.Grow(half));
                }
            }
            double othersOv = scene.ObstaclesNear(b, exempt).Sum(o => o.AreaOver(b));
            double allOv = scene.ObstaclesNear(b).Sum(o => o.AreaOver(b));
            c += cfg.WGeometry * othersOv;
            c += cfg.WOwnLine * Math.Max(0, allOv - othersOv);
            double outside = scene.OutsideAllowed(b);
            if (outside > 1e-9) c += cfg.WFrame * outside;
        }

        var leadBox = lead.Box(cfg.Clearance, ps[u.Lead]);
        c += cfg.WLeader * LeaderCrossings(scene, cfg, lead, leadBox,
                                           boxes, memberSet, exempt);
        return c;
    }

    static double UnitCost(Scene scene, Tuning cfg, Unit u, Vec2 pos, Rect[] boxes)
    {
        double c = UnitTrouble(scene, cfg, u, pos, boxes);

        // ISOGEN's placement is the PRIOR, not a defect: only drifting
        // FARTHER from the part than ISOGEN left it is charged.
        var lead = scene.Labels[u.Members[u.Lead]];
        var leadBox = lead.Box(0, u.Positions(pos)[u.Lead]);
        double excess = leadBox.Distance(u.Anchor) - u.HomeAnchorDist;
        if (excess > 0) c += cfg.WDistance * excess;

        double d = (pos - u.Home).Len();
        if (d > 1e-9) c += cfg.WStay + cfg.WDisplacement * d;
        return c;
    }
}
