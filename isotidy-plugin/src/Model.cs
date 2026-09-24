// The geometric model the solver works on -- the C# twin of isotidy/model.py.
// Nothing in this file (or Detect/Solve/FreeSpace) touches an AutoCAD type:
// ObjectIds are carried as opaque handles for write-back only.

using Autodesk.AutoCAD.DatabaseServices;

namespace IsoTidy;

public sealed class Label
{
    public int Index;
    public ObjectId Id;                // CAD residue, used only on write-back
    public string Text = "";
    public string Cls = "other";       // annotation | continuation | dimension | balloon
    public string Kind = "text";       // text | dimension | balloon
    public Vec2 Anchor;                // the point this label describes
    public Vec2 Size;                  // (width, height)
    public Vec2 Pos;                   // CURRENT lower-left
    public Vec2 Home;                  // original lower-left, never mutated
    public Vec2? Slide;                // unit vector when text may only slide
    public double? SlideLimit;         // audit's drift bar -- see Extract
    public ObjectId LeaderId = ObjectId.Null;   // its MULTILEADER, if any
    public bool DrawnLeader;           // a previous IsoTidy run drew a LINE leader
    public int? Group;                 // welded callouts share a group id

    public Rect Box(double pad = 0, Vec2? at = null) =>
        Rect.At(at ?? Pos, Size, pad);
    public Vec2 Center(Vec2? at = null)
    {
        var p = at ?? Pos;
        return new Vec2(p.X + Size.X / 2, p.Y + Size.Y / 2);
    }
    public Vec2 Delta() => Pos - Home;
    public double Displacement() => Delta().Len();
    public bool Moved(double eps = 1e-6) => Displacement() > eps;
    public double AnchorDistance(Vec2? at = null) => Box(0, at).Distance(Anchor);
}

/// An obstacle: either a drawn ribbon (Path != null) or a solid box.
/// Owner marks geometry that IS part of some annotation (a dimension's own
/// lines, a callout's own leader) so scoring can skip exactly the self-pairs.
public sealed class Obstacle
{
    public Poly Path;                  // null => solid box
    public Rect Box;
    public ObjectId Owner = ObjectId.Null;

    public Rect Bounds => Path?.Bounds ?? Box;
    public double AreaOver(Rect r) =>
        Path != null ? Path.AreaOver(r) : Box.IntersectionArea(r);
    public bool CrossesSeg(Vec2 a, Vec2 b) =>
        Path != null ? Path.CrossesSeg(a, b)
                     : Geo.SegLenInRect(a, b, Box) > 1e-9;
    public double Distance(Vec2 p) =>
        Path?.Distance(p) ?? Box.Distance(p);
}

public sealed class Scene
{
    public List<Label> Labels = new();
    public List<Obstacle> Obstacles = new();
    public List<Poly> Ink = new();          // real drawn curves, ribbon width
    public Rect? Sheet;                     // drawable region (frame margin applied)
    public List<Rect> Reserved = new();     // BOM tables etc. inside the sheet
    public FreeSpace Free;                  // occupancy grid, may be null
    public Dictionary<string, int> UnmappedText = new();

    public double InkUnder(Rect box)
    {
        double total = 0;
        foreach (var p in Ink)
            total += p.AreaOver(box);
        return total;
    }

    public IEnumerable<Obstacle> ObstaclesNear(Rect r, HashSet<ObjectId> exempt = null)
    {
        foreach (var o in Obstacles)
        {
            if (!o.Bounds.Intersects(r)) continue;
            if (exempt != null && !o.Owner.IsNull && exempt.Contains(o.Owner)) continue;
            yield return o;
        }
    }

    public HashSet<ObjectId> OwnExempt(Label lab)
    {
        var members = lab.Group is int g
            ? Labels.Where(l => l.Group == g) : new[] { lab };
        var set = new HashSet<ObjectId>();
        foreach (var m in members)
        {
            set.Add(m.Id);
            if (!m.LeaderId.IsNull) set.Add(m.LeaderId);
        }
        return set;
    }

    /// Area of `box` outside the drawable region plus inside reserved blocks.
    public double OutsideAllowed(Rect box)
    {
        if (Sheet is not Rect sheet) return 0;
        double outside = box.Area - box.IntersectionArea(sheet);
        foreach (var r in Reserved)
            outside += box.IntersectionArea(r);
        return outside;
    }

    /// Label indices bundled into things that move as one (deterministic order).
    public List<int[]> RigidUnits()
    {
        var singles = new List<int[]>();
        var groups = new Dictionary<int, List<int>>();
        foreach (var l in Labels)
        {
            if (l.Group is int g)
            {
                if (!groups.TryGetValue(g, out var list))
                    groups[g] = list = new List<int>();
                list.Add(l.Index);
            }
            else singles.Add([l.Index]);
        }
        var all = singles.Concat(groups.Values.Select(v => v.ToArray())).ToList();
        all.Sort((a, b) => a[0].CompareTo(b[0]));
        return all;
    }

    public bool SameGroup(int i, int j) =>
        Labels[i].Group is int g && Labels[j].Group == g;
}
