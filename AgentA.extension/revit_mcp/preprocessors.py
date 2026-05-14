# -*- coding: utf-8 -*-
import Autodesk.Revit.DB as DB

class HideJoinFailuresPreprocessor(DB.IFailuresPreprocessor):
    """
    Custom failure preprocessor to handle AutoJoin and other common BIM failures.
    """
    __namespace__ = "MCP_HideJoin_v1"

    
    def __init__(self, doc=None):
        """Initialize with optional document reference."""
        self.doc = doc
    
    def PreprocessFailures(self, failuresAccessor):
        """
        Process failures and suppress non-critical ones for better performance.
        Returns FailureProcessingResult to tell Revit how to proceed.
        """
        try:
            failure_messages = failuresAccessor.GetFailureMessages()
            
            for failure in failure_messages:
                failure_def_id = failure.GetFailureDefinitionId()
                severity = failure.GetSeverity()
                
                # Define failure types to suppress (AutoJoin, overlap, and room boundary issues)
                suppressed_failures = [
                    DB.BuiltInFailures.JoinElementsFailures.AttemptedJoinFailed,
                    DB.BuiltInFailures.JoinElementsFailures.CannotJoinElementsError,
                    DB.BuiltInFailures.JoinElementsFailures.CannotJoinElementsWarning,
                    DB.BuiltInFailures.GeneralFailures.DuplicateValue,
                    DB.BuiltInFailures.OverlapFailures.WallsOverlap,
                    DB.BuiltInFailures.OverlapFailures.FloorsOverlap,
                    DB.BuiltInFailures.OverlapFailures.CurvesOverlap,
                    DB.BuiltInFailures.OverlapFailures.WallRoomSeparationOverlap,
                ]

                # Room boundary warnings (walls overlap for room boundaries)
                try:
                    suppressed_failures.append(DB.BuiltInFailures.RoomFailures.RoomBoundaryLinesOverlap)
                except AttributeError:
                    pass  # Not available in all Revit versions
                
                # Suppress warnings and errors that are safe to ignore
                if failure_def_id in suppressed_failures:
                    if severity == DB.FailureSeverity.Warning:
                        failuresAccessor.DeleteWarning(failure)
                    elif severity == DB.FailureSeverity.Error:
                        # Try to resolve with default resolution first
                        if failure.HasResolutions():
                            try:
                                failuresAccessor.ResolveFailure(failure, failure.GetDefaultResolutionIndex())
                            except:
                                failuresAccessor.DeleteWarning(failure)
                        else:
                            failuresAccessor.DeleteWarning(failure)
                else:
                    # For other failures, try default resolution
                    if severity == DB.FailureSeverity.Warning:
                        failuresAccessor.DeleteWarning(failure)
                    elif failure.HasResolutions():
                        try:
                            failuresAccessor.ResolveFailure(failure, failure.GetDefaultResolutionIndex())
                        except:
                            pass
            
            return DB.FailureProcessingResult.Continue
            
        except Exception:
            # If anything goes wrong, continue rather than breaking the transaction
            return DB.FailureProcessingResult.Continue

class NuclearJoinGuard(DB.IFailuresPreprocessor):
    """
    NEVER PERMIT JOINS: The most aggressive preprocessor for large-scale builds.
    Forces all join attempts to be ignored or deleted without calculation.
    """
    __namespace__ = "MCP_Nuclear_v1"
    def __init__(self, doc=None):
        self.doc = doc

    def PreprocessFailures(self, failuresAccessor):
        try:
            failure_messages = failuresAccessor.GetFailureMessages()
            for failure in failure_messages:
                severity = failure.GetSeverity()
                # Nuclear Option: delete ALL warnings unconditionally.
                # This catches join, overlap, line-overlap, wall-overlap,
                # and any other non-critical warning that would block
                # the procedural generation process.
                if severity == DB.FailureSeverity.Warning:
                    failuresAccessor.DeleteWarning(failure)
                elif severity == DB.FailureSeverity.Error:
                    if failure.HasResolutions():
                        try:
                            failuresAccessor.ResolveFailure(failure, failure.GetDefaultResolutionIndex())
                        except:
                            pass

            return DB.FailureProcessingResult.Continue
        except:
            return DB.FailureProcessingResult.Continue
