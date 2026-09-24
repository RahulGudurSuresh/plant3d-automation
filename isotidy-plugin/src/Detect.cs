// Collision detection and scoring -- the C# twin of isotidy/detect.py.
// This is the SOURCE OF TRUTH and the referee the solver is graded against;
// it deliberately knows nothing about the solver (see the scorekeeper veto).

namespace IsoTidy;

public readonly record struct Collision(int A, int? B, string Kind, double Area);

public sealed class Report
{
    public int NLabels;
    public List<Collision> Collisions = new();
    public double OverlapArea => Collisions.Sum(c => c.Area);
    public bool Clean => Collisions.Count == 0;

    public string Summary(string title)
    {
        double lab = Collisions.Where(c => c.Kind == "label").Sum(c => c.Area);
        double geo = Collisions.Where(c => c.Kind == "geometry").Sum(c => c.Area);
        double frm = Collisions.Where(c => c.Kind == "frame").Sum(c => c.Area);
        int nl = Collisions.Count(c => c.Kind == "label");
        int ng = Collisions.Count(c => c.Kind == "geometry");
        int nf = Collisions.Count(c => c.Kind == "frame");
        var involved = new HashSet<int>();
        foreach (var c in Collisions) { involved.Add(c.A); if (c.B is int b) involved.Add(b); }
        return $"  {title}\n" +
               $"    label <-> label     {nl,4} pairs   {lab,8:F2} mm2\n" +
               $"    label <-> geometry  {ng,4} hits    {geo,8:F2} mm2\n" +
               $"    label <-> frame     {nf,4} hits    {frm,8:F2} mm2\n" +
               $"    labels affected     {involved.Count,4} / {NLabels}\n" +
               $"    total overlap       {OverlapArea,8:F2} mm2";
    }
}

public static class Detector
{
    public static Report Detect(Scene scene, Tuning cfg)
    {
        var report = new Report { NLabels = scene.Labels.Count };
        var boxes = scene.Labels.Select(l => l.Box(cfg.Clearance)).ToArray();

        // label <-> label.  A balloon tucked on (or stacked with) its own
        // text is ONE annotation drawn the way ISOGEN draws it, not two
        // things colliding -- group members are never scored against each other.
        for (int i = 0; i < boxes.Length; i++)
            for (int j = i + 1; j < boxes.Length; j++)
            {
                if (scene.SameGroup(i, j)) continue;
                double a = boxes[i].IntersectionArea(boxes[j]);
                if (a > 0)
                    report.Collisions.Add(new Collision(i, j, "label", a));
            }

        // label <-> fixed geometry, own annotation exempt (a dimension's own
        // line, a callout's own leader are convention, not defects).
        for (int i = 0; i < boxes.Length; i++)
        {
            var exempt = scene.OwnExempt(scene.Labels[i]);
            double a = scene.ObstaclesNear(boxes[i], exempt)
                            .Sum(o => o.AreaOver(boxes[i]));
            if (a > 0)
                report.Collisions.Add(new Collision(i, null, "geometry", a));
        }

        // label <-> frame -- a hard constraint, not a preference.
        for (int i = 0; i < boxes.Length; i++)
        {
            double outside = scene.OutsideAllowed(boxes[i]);
            if (outside > 1e-9)
                report.Collisions.Add(new Collision(i, null, "frame", outside));
        }

        return report;
    }
}
