// COMPILE-CHECK STUBS ONLY.  Minimal shapes of the AutoCAD managed types
// the plugin touches, so `dotnet build` can catch C# errors in OUR code on
// a machine without AutoCAD.  Proves nothing about the real API surface --
// only the real acdbmgd/acmgd/accoremgd DLLs do that.
#pragma warning disable CS8618, CS0067
using System.Collections;

namespace Autodesk.AutoCAD.Geometry
{
    public struct Point3d
    {
        public double X, Y, Z;
        public Point3d(double x, double y, double z) { X = x; Y = y; Z = z; }
        public Point3d TransformBy(Matrix3d m) => this;
        public double DistanceTo(Point3d o) =>
            Math.Sqrt((X - o.X) * (X - o.X) + (Y - o.Y) * (Y - o.Y) + (Z - o.Z) * (Z - o.Z));
    }
    public struct Point2d { public double X, Y; public Point2d(double x, double y) { X = x; Y = y; } }
    public struct Vector3d
    {
        public double X, Y, Z;
        public Vector3d(double x, double y, double z) { X = x; Y = y; Z = z; }
    }
    public struct Matrix3d { public static Matrix3d Displacement(Vector3d v) => default; }
    public struct Scale3d
    {
        public double X, Y, Z;
        public Scale3d(double x, double y, double z) { X = x; Y = y; Z = z; }
    }
    public struct Extents3d { public Point3d MinPoint, MaxPoint; }
}

namespace Autodesk.AutoCAD.Colors
{
    public enum ColorMethod { ByAci }
    public class Color { public static Color FromColorIndex(ColorMethod m, short i) => new(); }
}

namespace Autodesk.AutoCAD.DatabaseServices
{
    using Autodesk.AutoCAD.Geometry;

    public struct ObjectId : IEquatable<ObjectId>
    {
        public static ObjectId Null => default;
        public bool IsNull => true;
        public bool Equals(ObjectId o) => true;
        public override bool Equals(object o) => o is ObjectId;
        public override int GetHashCode() => 0;
        public static bool operator ==(ObjectId a, ObjectId b) => true;
        public static bool operator !=(ObjectId a, ObjectId b) => false;
    }
    public enum OpenMode { ForRead, ForWrite }
    public enum DwgVersion { Current }

    public class DBObject : IDisposable
    {
        public ObjectId ObjectId => default;
        public void Dispose() { }
        public void Erase() { }
        public void UpgradeOpen() { }
    }
    public class DBObjectCollection : IEnumerable
    {
        public IEnumerator GetEnumerator() => new List<DBObject>().GetEnumerator();
    }
    public class Entity : DBObject
    {
        public string Layer { get; set; }
        public Extents3d GeometricExtents => default;
        public void TransformBy(Matrix3d m) { }
    }
    public class Curve : Entity
    {
        public double StartParam => 0; public double EndParam => 1;
        public Point3d GetPointAtParameter(double t) => default;
        public Point3d StartPoint => default; public Point3d EndPoint => default;
    }
    public class Line : Curve { public Line() { } public Line(Point3d a, Point3d b) { } }
    public class Polyline : Curve
    {
        public int NumberOfVertices => 0; public bool Closed => false;
        public Point2d GetPoint2dAt(int i) => default;
    }
    public class Circle : Curve { }
    public class Arc : Curve { }
    public class Ellipse : Curve { }
    public class Spline : Curve { }
    public class Solid : Entity { public Point3d GetPointAt(short i) => default; }
    public class MText : Entity { public string Text => ""; }
    public class DBText : Entity { public string TextString => ""; }
    public class Dimension : Entity
    {
        public Point3d TextPosition { get; set; }
        public bool UsingDefaultTextPosition { get; set; }
        public int Dimtmove { get; set; }
        public void RecomputeDimensionBlock(bool b) { }
        public ObjectId DimBlockId => default;
        public string DimensionText => "";
    }
    public class AttributeCollection : IEnumerable<ObjectId>
    {
        public int Count => 0;
        public IEnumerator<ObjectId> GetEnumerator() => new List<ObjectId>().GetEnumerator();
        IEnumerator IEnumerable.GetEnumerator() => GetEnumerator();
    }
    public class AttributeReference : Entity
    {
        public Point3d Position { get; set; }
        public string TextString { get; set; }
    }
    public class BlockReference : Entity
    {
        public Point3d Position { get; set; }
        public Scale3d ScaleFactors { get; set; }
        public double Rotation { get; set; }
        public string Name => "";
        public AttributeCollection AttributeCollection => new();
        public ObjectId BlockTableRecord => default;
        public ObjectId DynamicBlockTableRecord => default;
        public Matrix3d BlockTransform => default;
        public void Explode(DBObjectCollection c) { }
    }
    public class MLeader : Entity
    {
        public Point3d GetFirstVertex(int i) => default;
        public Point3d GetLastVertex(int i) => default;
        public void SetLastVertex(int i, Point3d p) { }
        public bool EnableDogleg { get; set; }
        public double DoglegLength { get; set; }
        public Vector3d GetDogleg(int i) => default;
    }
    public class SymbolTableRecord : DBObject { public string Name { get; set; } }
    public class BlockTableRecord : SymbolTableRecord, IEnumerable<ObjectId>
    {
        public static string ModelSpace => "*Model_Space";
        public ObjectId AppendEntity(Entity e) => default;
        public IEnumerator<ObjectId> GetEnumerator() => new List<ObjectId>().GetEnumerator();
        IEnumerator IEnumerable.GetEnumerator() => GetEnumerator();
    }
    public class LayerTableRecord : SymbolTableRecord
    {
        public Autodesk.AutoCAD.Colors.Color Color { get; set; }
    }
    public class BlockTable : DBObject { public ObjectId this[string name] => default; }
    public class LayerTable : DBObject
    {
        public bool Has(string n) => false;
        public ObjectId Add(LayerTableRecord r) => default;
    }
    public class Transaction : IDisposable
    {
        public DBObject GetObject(ObjectId id, OpenMode m) => new();
        public void AddNewlyCreatedDBObject(DBObject o, bool add) { }
        public void Commit() { }
        public void Abort() { }
        public void Dispose() { }
    }
    public class TransactionManager { public Transaction StartTransaction() => new(); }
    public class Database : IDisposable
    {
        public Database(bool buildDefault, bool noDocument) { }
        public ObjectId BlockTableId => default;
        public string Filename => "";
        public ObjectId LayerTableId => default;
        public TransactionManager TransactionManager => new();
        public void ReadDwgFile(string f, FileShare s, bool allowCP, string pw) { }
        public void SaveAs(string f, DwgVersion v) { }
        public void Dispose() { }
    }
}

namespace Autodesk.AutoCAD.EditorInput
{
    public enum PromptStatus { OK, Cancel }
    public class PromptResult { public PromptStatus Status => PromptStatus.OK; public string StringResult => ""; }
    public class Editor
    {
        public void WriteMessage(string s) { }
        public PromptResult GetString(string prompt) => new();
    }
}

namespace Autodesk.AutoCAD.ApplicationServices
{
    public class Document
    {
        public Autodesk.AutoCAD.EditorInput.Editor Editor => new();
        public Autodesk.AutoCAD.DatabaseServices.Database Database => new(false, true);
        public IDisposable LockDocument() => new Autodesk.AutoCAD.DatabaseServices.Transaction();
    }
    public class DocumentCollection { public Document MdiActiveDocument => new(); }
    public static class Application { public static DocumentCollection DocumentManager => new(); }
}

namespace Autodesk.AutoCAD.Runtime
{
    [Flags] public enum CommandFlags { Modal = 0, Session = 2 }
    [AttributeUsage(AttributeTargets.Method)]
    public class CommandMethodAttribute : Attribute
    {
        public CommandMethodAttribute(string name, CommandFlags flags) { }
    }
    [AttributeUsage(AttributeTargets.Assembly, AllowMultiple = true)]
    public class CommandClassAttribute : Attribute { public CommandClassAttribute(Type t) { } }
}
