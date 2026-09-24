// Minimal 2D geometry, self-contained (no NetTopologySuite: the target
// machine is air-gapped, so the plugin depends on nothing outside the BCL).
//
// Labels are axis-aligned rectangles and obstacles are polylines with a
// half-width, so the only areas ever needed are rect-rect intersections
// (exact) and rect-vs-buffered-polyline (approximated as clipped segment
// length x ribbon width -- corner effects are below the scoring noise floor;
// exact parity with the Python/shapely reference is checked in validation,
// not assumed here).

namespace IsoTidy;

public readonly struct Vec2(double x, double y)
{
    public readonly double X = x, Y = y;
    public static Vec2 operator +(Vec2 a, Vec2 b) => new(a.X + b.X, a.Y + b.Y);
    public static Vec2 operator -(Vec2 a, Vec2 b) => new(a.X - b.X, a.Y - b.Y);
    public double Dot(Vec2 o) => X * o.X + Y * o.Y;
    public double Len() => Math.Sqrt(X * X + Y * Y);
    public override string ToString() => $"({X:F2},{Y:F2})";
}

public readonly struct Rect(double x0, double y0, double x1, double y1)
{
    public readonly double X0 = Math.Min(x0, x1), Y0 = Math.Min(y0, y1),
                           X1 = Math.Max(x0, x1), Y1 = Math.Max(y0, y1);
    public double W => X1 - X0;
    public double H => Y1 - Y0;
    public double Area => W * H;
    public Vec2 Center => new((X0 + X1) / 2, (Y0 + Y1) / 2);

    public static Rect At(Vec2 pos, Vec2 size, double pad = 0) =>
        new(pos.X - pad, pos.Y - pad, pos.X + size.X + pad, pos.Y + size.Y + pad);

    public Rect Grow(double d) => new(X0 - d, Y0 - d, X1 + d, Y1 + d);

    public bool Intersects(Rect o) =>
        X0 < o.X1 && o.X0 < X1 && Y0 < o.Y1 && o.Y0 < Y1;

    public double IntersectionArea(Rect o)
    {
        double w = Math.Min(X1, o.X1) - Math.Max(X0, o.X0);
        double h = Math.Min(Y1, o.Y1) - Math.Max(Y0, o.Y0);
        return (w > 0 && h > 0) ? w * h : 0.0;
    }

    public bool Contains(Vec2 p) => p.X >= X0 && p.X <= X1 && p.Y >= Y0 && p.Y <= Y1;

    public double Distance(Vec2 p)
    {
        double dx = Math.Max(Math.Max(X0 - p.X, 0), p.X - X1);
        double dy = Math.Max(Math.Max(Y0 - p.Y, 0), p.Y - Y1);
        return Math.Sqrt(dx * dx + dy * dy);
    }

    public double Distance(Rect o)
    {
        double dx = Math.Max(Math.Max(X0 - o.X1, 0), o.X0 - X1);
        double dy = Math.Max(Math.Max(Y0 - o.Y1, 0), o.Y0 - Y1);
        return Math.Sqrt(dx * dx + dy * dy);
    }

    /// Nearest point on this rect's boundary-or-interior to p.
    public Vec2 Nearest(Vec2 p) =>
        new(Math.Clamp(p.X, X0, X1), Math.Clamp(p.Y, Y0, Y1));
}

public static class Geo
{
    /// True segment-segment crossing (shared endpoints do not count).
    public static bool SegCross(Vec2 a, Vec2 b, Vec2 c, Vec2 d)
    {
        static double Turn(Vec2 p, Vec2 q, Vec2 r) =>
            (q.X - p.X) * (r.Y - p.Y) - (q.Y - p.Y) * (r.X - p.X);
        double t1 = Turn(a, b, c), t2 = Turn(a, b, d);
        double t3 = Turn(c, d, a), t4 = Turn(c, d, b);
        return t1 * t2 < 0 && t3 * t4 < 0;
    }

    public static double PointSegDist(Vec2 p, Vec2 a, Vec2 b)
    {
        Vec2 ab = b - a;
        double L2 = ab.Dot(ab);
        if (L2 < 1e-12) return (p - a).Len();
        double t = Math.Clamp((p - a).Dot(ab) / L2, 0, 1);
        return (p - new Vec2(a.X + ab.X * t, a.Y + ab.Y * t)).Len();
    }

    /// Length of segment a-b that lies inside rect r (Liang-Barsky clip).
    public static double SegLenInRect(Vec2 a, Vec2 b, Rect r)
    {
        double dx = b.X - a.X, dy = b.Y - a.Y;
        double t0 = 0, t1 = 1;
        Span<double> p = [-dx, dx, -dy, dy];
        Span<double> q = [a.X - r.X0, r.X1 - a.X, a.Y - r.Y0, r.Y1 - a.Y];
        for (int i = 0; i < 4; i++)
        {
            if (Math.Abs(p[i]) < 1e-12)
            {
                if (q[i] < 0) return 0;
                continue;
            }
            double t = q[i] / p[i];
            if (p[i] < 0) { if (t > t1) return 0; if (t > t0) t0 = t; }
            else { if (t < t0) return 0; if (t < t1) t1 = t; }
        }
        return (t1 - t0) * Math.Sqrt(dx * dx + dy * dy);
    }
}

/// A drawn curve as a point chain with a half-width ("ribbon").
public sealed class Poly
{
    public readonly Vec2[] Pts;
    public readonly double HalfWidth;
    public readonly Rect Bounds;

    public Poly(Vec2[] pts, double halfWidth)
    {
        Pts = pts;
        HalfWidth = halfWidth;
        double x0 = double.MaxValue, y0 = double.MaxValue,
               x1 = double.MinValue, y1 = double.MinValue;
        foreach (var p in pts)
        {
            x0 = Math.Min(x0, p.X); y0 = Math.Min(y0, p.Y);
            x1 = Math.Max(x1, p.X); y1 = Math.Max(y1, p.Y);
        }
        Bounds = new Rect(x0 - halfWidth, y0 - halfWidth,
                          x1 + halfWidth, y1 + halfWidth);
    }

    /// Approximate overlap area with rect: clipped length x ribbon width.
    public double AreaOver(Rect r)
    {
        if (!Bounds.Intersects(r)) return 0;
        Rect grown = r.Grow(HalfWidth);
        double len = 0;
        for (int i = 0; i + 1 < Pts.Length; i++)
            len += Geo.SegLenInRect(Pts[i], Pts[i + 1], grown);
        return len * 2 * HalfWidth;
    }

    /// Does the ribbon's centreline pass through the rect at all?
    public bool Crosses(Rect r)
    {
        if (!Bounds.Intersects(r)) return false;
        for (int i = 0; i + 1 < Pts.Length; i++)
            if (Geo.SegLenInRect(Pts[i], Pts[i + 1], r) > 1e-9) return true;
        return Pts.Length == 1 && r.Contains(Pts[0]);
    }

    public bool CrossesSeg(Vec2 a, Vec2 b)
    {
        for (int i = 0; i + 1 < Pts.Length; i++)
            if (Geo.SegCross(a, b, Pts[i], Pts[i + 1])) return true;
        return false;
    }

    public double Distance(Vec2 p)
    {
        double best = double.MaxValue;
        for (int i = 0; i + 1 < Pts.Length; i++)
            best = Math.Min(best, Geo.PointSegDist(p, Pts[i], Pts[i + 1]));
        if (Pts.Length == 1) best = (p - Pts[0]).Len();
        return Math.Max(0, best - HalfWidth);
    }
}
