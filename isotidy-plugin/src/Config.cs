// Configuration: layer roles and solver tuning.
// EVERY NUMBER HERE MIRRORS isotidy/config.py IN THE PYTHON REFERENCE.
// Change them there first, prove the change on the fleet, then mirror it
// here -- never the other way round.  The Python pipeline is the R&D bench;
// this plugin is the delivery vehicle.

namespace IsoTidy;

public sealed class LayerRoles
{
    public Dictionary<string, string> Movable = new();
    public HashSet<string> Fixed = new();
    public HashSet<string> Frame = new();
    public HashSet<string> Constrained = new();
    public string Leader = "ISOTIDY_LEADER";
    public string[] BalloonPrefixes = [];
    public bool ReadDimensions;

    public bool IsMovable(string layer) => Movable.ContainsKey(layer);
    public string LabelClass(string layer) =>
        Movable.TryGetValue(layer, out var c) ? c : "other";
    public bool IsBalloon(string blockName) =>
        BalloonPrefixes.Any(p => blockName.StartsWith(p, StringComparison.Ordinal));

    /// Project JP1071 -- measured from the
    /// delivered DWGs.  A new client is a new instance of this, not new code.
    public static LayerRoles Jp1071() => new()
    {
        Movable = new()
        {
            ["Annotation"] = "annotation",
            ["Continuation"] = "continuation",
            ["Dimension"] = "dimension",
        },
        Fixed = ["Pipe", "Valve", "Symbol", "Weld", "Supports",
                 "Instruments", "Insulation Line", "Fitting", "Flange",
                 // seen 2026-09-17 on the difficult_iso sheet
                 "Heat Tracing Line", "Skew Box"],
        Frame = ["0"],
        Constrained = ["Dimension"],
        Leader = "ISOTIDY_LEADER",
        BalloonPrefixes = ["Anno"],
        ReadDimensions = true,
    };
}

public sealed class Tuning
{
    // --- geometry --------------------------------------------------------
    public double Clearance = 0.8;
    public double GeomBuffer = 0.35;
    public double InkHalfwidth = 0.35;
    public double InkTolerance = 0.5;      // the "never make a spot worse" veto slack
    public double FrameMargin = 2.0;
    public double PairGap = 2.0;
    public double StackAlign = 0.3;        // callout stacks: left-edge alignment
    public double StackGap = 4.0;          //   and max vertical gap between members
    public double TuckRelief = 2.5;

    // --- dimension drift (shared bar with the audit) ---------------------
    public const double DimDriftFloor = 3.0;
    public const double DimDriftFactor = 2.0;
    public const double SlideLimitMargin = 0.3;

    // --- candidate generation --------------------------------------------
    public double[] Radii = [3.0, 6.0, 10.0, 15.0];
    public int NDirections = 12;           // 30-degree steps: includes iso axes
    public double FreeCell = 0.5;
    public double FreeStep = 2.0;
    public double FreeReach = 30.0;
    public int FreeSlots = 24;
    public double FreeHalo = 1.5;
    public double[] SlideSteps = [0.0, 4.0, -4.0, 8.0, -8.0, 13.0, -13.0];

    // --- cost weights (every one swept, see config.py for the tables) ----
    public double WLabel = 120.0;
    public double WGeometry = 80.0;
    public double ProximityBand = 2.0;
    public double WProximity = 60.0;
    public double WOwnLine = 60.0;
    public double WFrame = 1.0e6;
    public double WDistance = 12.0;
    public double WStay = 10.0;
    public double WDisplacement = 16.0;
    public double WLeader = 180.0;

    // --- search ----------------------------------------------------------
    public int MaxPasses = 12;
    public double LeaderThreshold = 3.5;

    public const double FarFromPart = 25.0;  // audit category-C bar
}
