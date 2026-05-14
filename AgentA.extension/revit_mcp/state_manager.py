# -*- coding: utf-8 -*-
import clr

# --- EXTENSIBLE STORAGE STATE MANAGER ---
# Uses Revit's Extensible Storage framework to store "hidden" 
# metadata on AI-generated elements.

class StateManager:
    SCHEMA_GUID = "B6D5A8C1-F8B4-406F-9D6A-7E5C4B4C1234" # Unique for this AI
    
    def __init__(self):
        self._schema = None

    def get_schema(self):
        """Returns the Revit Schema for AI metadata."""
        import Autodesk.Revit.DB as DB # type: ignore
        from System import Guid
        
        if self._schema:
            return self._schema
            
        schema = DB.ExtensibleStorage.Schema.Lookup(Guid(self.SCHEMA_GUID))
        if not schema:
            builder = DB.ExtensibleStorage.SchemaBuilder(Guid(self.SCHEMA_GUID))
            builder.SetReadAccessLevel(DB.ExtensibleStorage.AccessLevel.Public)
            builder.SetWriteAccessLevel(DB.ExtensibleStorage.AccessLevel.Public)
            builder.SetSchemaName("AI_Metadata")
            
            # Fields
            import System
            builder.AddSimpleField("AI_ID", System.String)
            builder.AddSimpleField("GeometryHash", System.String)
            builder.AddSimpleField("SchemaVersion", System.Int32)
            
            schema = builder.Finish()
            
        self._schema = schema
        return schema

    def set_ai_metadata(self, element, ai_id, geometry_hash="", version=1):
        """Attaches AI metadata to a Revit element."""
        import Autodesk.Revit.DB as DB # type: ignore
        schema = self.get_schema()
        entity = DB.ExtensibleStorage.Entity(schema)
        
        import System
        entity.Set[System.String]("AI_ID", str(ai_id))
        entity.Set[System.String]("GeometryHash", str(geometry_hash))
        entity.Set[System.Int32]("SchemaVersion", int(version))
        
        # We need a Transaction to SetEntity, but we assume we are inside one
        element.SetEntity(entity)

    def get_ai_metadata(self, element):
        """Reads AI metadata from a Revit element."""
        import Autodesk.Revit.DB as DB # type: ignore
        schema = self.get_schema()
        entity = element.GetEntity(schema)
        
        if not entity.IsValid():
            return None
            
        import System
        return {
            "ai_id": entity.Get[System.String]("AI_ID"),
            "hash": entity.Get[System.String]("GeometryHash"),
            "version": entity.Get[System.Int32]("SchemaVersion")
        }

# Global instance
state_manager = StateManager()
