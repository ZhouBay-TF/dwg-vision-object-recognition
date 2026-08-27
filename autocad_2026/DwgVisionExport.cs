using System.Text.Json;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;

[assembly: CommandClass(typeof(DwgVisionExport.Commands))]

namespace DwgVisionExport;

public sealed class Commands
{
    [CommandMethod("DWGVISION_EXPORT", CommandFlags.Session)]
    public void ExportCurrentDrawing()
    {
        Document document = Application.DocumentManager.MdiActiveDocument;
        Editor editor = document.Editor;
        PromptStringOptions prompt = new("\nJSON output path: ")
        {
            AllowSpaces = true,
        };
        PromptResult result = editor.GetString(prompt);
        if (result.Status != PromptStatus.OK || string.IsNullOrWhiteSpace(result.StringResult))
        {
            editor.WriteMessage("\nDWGVISION_EXPORT cancelled.");
            return;
        }

        Database database = document.Database;
        List<Dictionary<string, object?>> entities = [];
        List<Dictionary<string, object?>> rawEntities = [];
        List<Dictionary<string, object?>> annotations = [];
        List<Dictionary<string, object?>> frames = [];
        using (Transaction transaction = database.TransactionManager.StartTransaction())
        {
            BlockTable blockTable = (BlockTable)transaction.GetObject(database.BlockTableId, OpenMode.ForRead);
            BlockTableRecord modelSpace = (BlockTableRecord)transaction.GetObject(
                blockTable[BlockTableRecord.ModelSpace], OpenMode.ForRead);
            List<(string Name, BlockTableRecord Record)> spaces = [("ModelSpace", modelSpace)];
            try
            {
                DBDictionary layoutDictionary = (DBDictionary)transaction.GetObject(
                    database.LayoutDictionaryId, OpenMode.ForRead);
                foreach (DBDictionaryEntry entry in layoutDictionary)
                {
                    Layout layout = (Layout)transaction.GetObject(entry.Value, OpenMode.ForRead);
                    if (layout.ModelType)
                    {
                        continue;
                    }
                    BlockTableRecord paperSpace = (BlockTableRecord)transaction.GetObject(
                        layout.BlockTableRecordId, OpenMode.ForRead);
                    spaces.Add(($"PaperSpace:{entry.Key}", paperSpace));
                }
            }
            catch
            {
                // A malformed/unsupported layout must not prevent ModelSpace export.
            }

            foreach ((string spaceName, BlockTableRecord spaceRecord) in spaces)
            {
                foreach (ObjectId objectId in spaceRecord)
                {
                    if (transaction.GetObject(objectId, OpenMode.ForRead) is not Entity entity)
                    {
                        continue;
                    }

                    string layer = entity.Layer ?? string.Empty;
                string? type = Classify(layer);
                string handle = objectId.Handle.ToString();
                    Dictionary<string, object?> provenance = new()
                    {
                        ["source"] = "autocad_2026_dotnet",
                        ["handle"] = handle,
                        ["layer"] = layer,
                        ["object_name"] = entity.GetType().Name,
                        ["space"] = spaceName,
                    };

                if (entity is DBText dbText)
                {
                    annotations.Add(new Dictionary<string, object?>
                    {
                        ["text_id"] = handle,
                        ["handle"] = handle,
                        ["text"] = dbText.TextString,
                        ["bbox_world"] = TryExtents(entity, out Extents3d textExtents) ? Box(textExtents) : null,
                        ["position_world"] = new[] { dbText.Position.X, dbText.Position.Y },
                        ["height"] = dbText.Height,
                        ["rotation_deg"] = dbText.Rotation * 180.0 / Math.PI,
                        ["source"] = "autocad_2026_dotnet",
                        ["layer"] = layer,
                    });
                }
                else if (entity is MText mText)
                {
                    annotations.Add(new Dictionary<string, object?>
                    {
                        ["text_id"] = handle,
                        ["handle"] = handle,
                        ["text"] = mText.Text,
                        ["bbox_world"] = TryExtents(entity, out Extents3d textExtents) ? Box(textExtents) : null,
                        ["position_world"] = new[] { mText.Location.X, mText.Location.Y },
                        ["height"] = mText.TextHeight,
                        ["rotation_deg"] = mText.Rotation * 180.0 / Math.PI,
                        ["source"] = "autocad_2026_dotnet",
                        ["layer"] = layer,
                    });
                }
                else if (entity is Dimension dimension)
                {
                    annotations.Add(new Dictionary<string, object?>
                    {
                        ["text_id"] = handle,
                        ["handle"] = handle,
                        ["text"] = dimension.DimensionText,
                        ["bbox_world"] = TryExtents(entity, out Extents3d dimensionExtents) ? Box(dimensionExtents) : null,
                        ["position_world"] = new[] { dimension.TextPosition.X, dimension.TextPosition.Y },
                        ["source"] = "autocad_2026_dotnet",
                        ["layer"] = layer,
                        ["role"] = "dimension",
                    });
                }

                if (entity is BlockReference blockReference)
                {
                    BlockTableRecord definition = (BlockTableRecord)transaction.GetObject(
                        blockReference.BlockTableRecord, OpenMode.ForRead);
                    string blockName = definition.Name;
                    type = Classify(layer + " " + blockName);
                    if (type is not null && TryExtents(entity, out Extents3d blockExtents))
                    {
                        entities.Add(MakeEntity(
                            type,
                            blockName,
                            blockExtents,
                            blockReference.Rotation * 180.0 / Math.PI,
                            provenance,
                            new Dictionary<string, object?> { ["block_name"] = blockName }));
                    }
                    if (TryExtents(entity, out Extents3d rawBlockExtents))
                    {
                        Dictionary<string, object?> raw = MakeRawEntity(
                            entity,
                            type,
                            blockName,
                            "block",
                            rawBlockExtents,
                            blockReference.Rotation * 180.0 / Math.PI,
                            provenance,
                            new Dictionary<string, object?> { ["rotation_deg"] = blockReference.Rotation * 180.0 / Math.PI });
                        rawEntities.Add(raw);
                        AddFrameCandidate(frames, raw);
                    }
                }
                else if (entity is not DBText && entity is not MText && entity is not Dimension && TryExtents(entity, out Extents3d extents))
                {
                    Dictionary<string, object?> dimensions = new();
                    List<List<double>> polygon = [];
                    string command = entity.GetType().Name.ToLowerInvariant();
                    if (entity is Line line)
                    {
                        double length = line.StartPoint.DistanceTo(line.EndPoint);
                        dimensions["length"] = length;
                        polygon = [[line.StartPoint.X, line.StartPoint.Y], [line.EndPoint.X, line.EndPoint.Y]];
                        command = "line";
                    }
                    else if (entity is Polyline polyline)
                    {
                        dimensions["vertex_count"] = polyline.NumberOfVertices;
                        dimensions["closed"] = polyline.Closed;
                        for (int index = 0; index < polyline.NumberOfVertices; index++)
                        {
                            Point2d point = polyline.GetPoint2dAt(index);
                            polygon.Add([point.X, point.Y]);
                        }
                        command = "polyline";
                    }
                    else if (entity is Circle circle)
                    {
                        dimensions["radius"] = circle.Radius;
                        command = "circle";
                    }
                    else if (entity is Arc arc)
                    {
                        dimensions["radius"] = arc.Radius;
                        dimensions["start_angle"] = arc.StartAngle;
                        dimensions["end_angle"] = arc.EndAngle;
                        command = "arc";
                    }
                    else if (entity is Ellipse ellipse)
                    {
                        dimensions["major_radius"] = ellipse.MajorRadius;
                        dimensions["minor_radius"] = ellipse.MinorRadius;
                        command = "ellipse";
                    }
                    Dictionary<string, object?> raw = MakeRawEntity(entity, type, entity.GetType().Name, command, extents, 0.0, provenance, dimensions, polygon);
                    rawEntities.Add(raw);
                    AddFrameCandidate(frames, raw);
                    if (type is not null && !(raw["is_frame"] as bool? ?? false))
                    {
                        entities.Add(MakeEntity(type, entity.GetType().Name, extents, 0.0, provenance, dimensions, polygon));
                    }
                }
                }
            }
            transaction.Commit();
        }

        Extents3d? drawingExtents = null;
        try
        {
            drawingExtents = database.Extmax.DistanceTo(database.Extmin) > 0
                ? new Extents3d(database.Extmin, database.Extmax)
                : null;
        }
        catch
        {
            // The drawing can have no valid extents yet; the JSON remains usable.
        }

        Dictionary<string, object?> payload = new()
        {
            ["schema_version"] = "dwg_raw.v1",
            ["adapter"] = "autocad_2026_dotnet",
            ["coordinate_space"] = "world",
            ["coordinate_system"] = new Dictionary<string, object?>
            {
                ["space"] = "world",
                ["units"] = "drawing_units",
                ["world_bounds"] = drawingExtents is null ? null : Box(drawingExtents.Value),
            },
            ["entities"] = entities,
            ["raw_entities"] = rawEntities,
            ["annotations"] = annotations,
            ["frames"] = frames,
        };
        string json = JsonSerializer.Serialize(payload, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(result.StringResult.Trim('"'), json);
        editor.WriteMessage($"\nExported {entities.Count} entities and {annotations.Count} labels.");
    }

    private static Dictionary<string, object?> MakeEntity(
        string type,
        string subtype,
        Extents3d extents,
        double rotation,
        Dictionary<string, object?> provenance,
        Dictionary<string, object?>? dimensions = null,
        List<List<double>>? polygon = null)
    {
        return new Dictionary<string, object?>
        {
            ["entity_id"] = provenance.TryGetValue("handle", out object? handle) ? handle : "",
            ["handle"] = provenance.TryGetValue("handle", out object? entityHandle) ? entityHandle : "",
            ["type"] = type,
            ["subtype"] = subtype,
            ["bbox_px"] = Box(extents),
            ["polygon_px"] = polygon ?? [],
            ["rotation_deg"] = rotation,
            ["dimensions"] = dimensions ?? new Dictionary<string, object?>(),
            ["confidence"] = 0.92,
            ["evidence"] = new Dictionary<string, object?> { ["method"] = "autocad_2026_dotnet" },
            ["provenance"] = provenance,
        };
    }

    private static Dictionary<string, object?> MakeRawEntity(
        Entity entity,
        string? type,
        string subtype,
        string command,
        Extents3d extents,
        double rotation,
        Dictionary<string, object?> provenance,
        Dictionary<string, object?>? dimensions = null,
        List<List<double>>? polygon = null)
    {
        string layer = entity.Layer ?? string.Empty;
        string objectName = entity.GetType().Name;
        bool isFrame = IsFrameLike(layer, subtype, objectName) &&
            (command == "block" || (command == "polyline" && dimensions?.TryGetValue("closed", out object? closed) == true && closed is true));
        return new Dictionary<string, object?>
        {
            ["entity_id"] = provenance.TryGetValue("handle", out object? handle) ? handle : "",
            ["handle"] = provenance.TryGetValue("handle", out object? entityHandle) ? entityHandle : "",
            ["type"] = type ?? "unknown",
            ["subtype"] = subtype,
            ["entity_type"] = objectName,
            ["command"] = command,
            ["bbox_world"] = Box(extents),
            ["polygon_world"] = polygon ?? [],
            ["dimensions"] = dimensions ?? new Dictionary<string, object?>(),
            ["layer"] = layer,
            ["color"] = entity.ColorIndex,
            ["is_frame"] = isFrame,
            ["provenance"] = provenance,
            ["rotation_deg"] = rotation,
        };
    }

    private static void AddFrameCandidate(List<Dictionary<string, object?>> frames, Dictionary<string, object?> raw)
    {
        if (raw["is_frame"] is not true)
        {
            return;
        }
        frames.Add(new Dictionary<string, object?>
        {
            ["scene_id"] = $"scene_frame_{frames.Count + 1:0000}",
            ["frame_type"] = "cad_frame_candidate",
            ["world_bbox"] = raw["bbox_world"],
            ["layout_id"] = null,
            ["source_entity_id"] = raw["entity_id"],
        });
    }

    private static bool IsFrameLike(string layer, string subtype, string objectName)
    {
        string value = $"{layer} {subtype} {objectName}".ToLowerInvariant();
        return ContainsAny(value, "frame", "border", "titleblock", "title_block", "图框", "图签", "viewport");
    }

    private static double[] Box(Extents3d extents) =>
    [extents.MinPoint.X, extents.MinPoint.Y, extents.MaxPoint.X, extents.MaxPoint.Y];

    private static bool TryExtents(Entity entity, out Extents3d extents)
    {
        try
        {
            extents = entity.GeometricExtents;
            return true;
        }
        catch
        {
            extents = default;
            return false;
        }
    }

    private static string? Classify(string value)
    {
        string lowered = value.ToLowerInvariant();
        if (ContainsAny(lowered, "wall", "a-wall", "墙", "墙体", "外墙", "内墙", "墙线")) return "wall";
        if (ContainsAny(lowered, "window", "窗", "a-glaz", "glaz")) return "window";
        if (ContainsAny(lowered, "door", "门", "入口", "a-door")) return "door";
        if (ContainsAny(lowered, "furniture", "furn", "家具", "bed", "sofa", "chair", "table", "desk", "cabinet", "床", "沙发", "椅", "桌", "柜")) return "furniture";
        return null;
    }

    private static bool ContainsAny(string value, params string[] terms) =>
        terms.Any(value.Contains);
}
