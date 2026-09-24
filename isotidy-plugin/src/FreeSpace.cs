// Occupancy grid + summed-area table -- direct port of isotidy/freespace.py.
// "Where does this box actually fit?" answered exactly, in constant time,
// so the solver can afford to ask thousands of times per label.
//
// Lines are stamped along their PATH, never by bounding box: an isometric is
// nothing but long diagonals, and the bbox of one diagonal is a large
// rectangle of mostly empty paper.

namespace IsoTidy;

public sealed class FreeSpace
{
    readonly double _x0, _y0, _cell;
    readonly int _nx, _ny;
    readonly byte[,] _occ;
    int[,] _sat;

    public FreeSpace(Rect bounds, double cell = 0.5)
    {
        _x0 = bounds.X0; _y0 = bounds.Y0; _cell = cell;
        _nx = Math.Max(1, (int)Math.Ceiling(bounds.W / cell));
        _ny = Math.Max(1, (int)Math.Ceiling(bounds.H / cell));
        _occ = new byte[_ny, _nx];
    }

    (int cx, int cy) Cells(double x, double y) =>
        ((int)((x - _x0) / _cell), (int)((y - _y0) / _cell));

    public void AddRect(Rect r, double grow = 0)
    {
        var (cx0, cy0) = Cells(r.X0 - grow, r.Y0 - grow);
        var (cx1, cy1) = Cells(r.X1 + grow, r.Y1 + grow);
        cx0 = Math.Max(0, cx0); cy0 = Math.Max(0, cy0);
        cx1 = Math.Min(_nx - 1, cx1); cy1 = Math.Min(_ny - 1, cy1);
        for (int y = cy0; y <= cy1; y++)
            for (int x = cx0; x <= cx1; x++)
                _occ[y, x] = 1;
    }

    /// Stamp a polyline, walked at half-cell steps so diagonals skip no cell.
    public void AddSegments(IReadOnlyList<Vec2> pts, double grow = 0)
    {
        int r = (int)Math.Ceiling(grow / _cell);
        double step = _cell * 0.5;
        for (int i = 0; i + 1 < pts.Count; i++)
        {
            double dx = pts[i + 1].X - pts[i].X, dy = pts[i + 1].Y - pts[i].Y;
            int n = Math.Max(1, (int)(Math.Sqrt(dx * dx + dy * dy) / step));
            for (int k = 0; k <= n; k++)
            {
                double t = (double)k / n;
                var (cx, cy) = Cells(pts[i].X + dx * t, pts[i].Y + dy * t);
                int x0 = Math.Max(0, cx - r), x1 = Math.Min(_nx - 1, cx + r);
                int y0 = Math.Max(0, cy - r), y1 = Math.Min(_ny - 1, cy + r);
                for (int y = y0; y <= y1; y++)
                    for (int x = x0; x <= x1; x++)
                        _occ[y, x] = 1;
            }
        }
    }

    public FreeSpace Finish()
    {
        _sat = new int[_ny, _nx];
        for (int y = 0; y < _ny; y++)
        {
            int row = 0;
            for (int x = 0; x < _nx; x++)
            {
                row += _occ[y, x];
                _sat[y, x] = row + (y > 0 ? _sat[y - 1, x] : 0);
            }
        }
        return this;
    }

    public int OccupiedIn(double x0, double y0, double x1, double y1)
    {
        if (_sat == null) Finish();
        var (cx0, cy0) = Cells(x0, y0);
        var (cx1, cy1) = Cells(x1, y1);
        cx0 = Math.Max(0, cx0); cy0 = Math.Max(0, cy0);
        cx1 = Math.Min(_nx - 1, cx1); cy1 = Math.Min(_ny - 1, cy1);
        if (cx1 < cx0 || cy1 < cy0) return 0;
        int total = _sat[cy1, cx1];
        if (cx0 > 0) total -= _sat[cy1, cx0 - 1];
        if (cy0 > 0) total -= _sat[cy0 - 1, cx1];
        if (cx0 > 0 && cy0 > 0) total += _sat[cy0 - 1, cx0 - 1];
        return total;
    }

    public bool Fits(Vec2 pos, Vec2 size, double pad = 0) =>
        OccupiedIn(pos.X - pad, pos.Y - pad,
                   pos.X + size.X + pad, pos.Y + size.Y + pad) == 0;

    /// Positions where the box genuinely fits, NEAREST HOME FIRST; roomy
    /// slots (clearing `halo` extra air) before tight ones at like distance.
    public List<Vec2> OpenSlots(Vec2 home, Vec2 size, double pad,
                                double maxReach, double step = 2.0,
                                int limit = 24, double halo = 1.5)
    {
        var found = new List<(double d, int tight, double x, double y)>();
        int n = (int)(maxReach / step);
        for (int i = -n; i <= n; i++)
            for (int j = -n; j <= n; j++)
            {
                double dx = i * step, dy = j * step;
                double d = Math.Sqrt(dx * dx + dy * dy);
                if (d < 1e-9 || d > maxReach) continue;
                var pos = new Vec2(home.X + dx, home.Y + dy);
                if (!Fits(pos, size, pad)) continue;
                int tight = Fits(pos, size, pad + halo) ? 0 : 1;
                found.Add((Math.Round(d, 3), tight, pos.X, pos.Y));
            }
        found.Sort();
        return found.Take(limit).Select(t => new Vec2(t.x, t.y)).ToList();
    }
}
