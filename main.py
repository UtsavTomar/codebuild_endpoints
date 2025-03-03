from fastapi import FastAPI, HTTPException, Depends, Query, status, Security, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Any, Dict
from datetime import datetime
import os
import psycopg2
from psycopg2.extras import DictCursor

# Configuration
DATABASE_URL = os.getenv("DB_CONNECTION_STRING")

def get_db_connection():
    if not DATABASE_URL:
        raise Exception("Database connection string not found")
    return psycopg2.connect(DATABASE_URL, cursor_factory=DictCursor)

# FastAPI app
app = FastAPI(title="Agent Build Status API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class BuildStatus(BaseModel):
    id: int
    agent_version_id: str
    build_id: str
    phase: Optional[str] = None
    step: str
    step_number: Optional[int] = None
    status: str
    message: str
    timestamp: datetime
    environment: str
    duration_ms: Optional[int] = None

class BuildStatusCreate(BaseModel):
    agent_version_id: str
    build_id: str
    phase: Optional[str] = None
    step: str
    step_number: Optional[int] = None
    status: str
    message: str
    timestamp: datetime = datetime.now()
    environment: str
    duration_ms: Optional[int] = None

class BuildInfo(BaseModel):
    agent_version_id: str
    agent_uuid: str
    version: str
    image_url: str

class BuildInfoResponse(BaseModel):
    id: int
    agent_version_id: str
    agent_uuid: str
    version: str
    image_url: str
    timestamp: datetime

class BuildStatusSummary(BaseModel):
    build_id: str
    environment: str
    overall_status: str
    phases: Dict[str, Dict[str, Any]]
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    total_duration_ms: Optional[int] = None

# Update the schema if needed
def ensure_schema_updated():
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        # Check if columns exist
        cursor.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_schema = 'agentic-platform' AND table_name = 'build_status' 
            AND column_name IN ('phase', 'step_number', 'duration_ms')
        """)
        existing_columns = [row[0] for row in cursor.fetchall()]
        
        # Add missing columns
        if 'phase' not in existing_columns:
            cursor.execute('ALTER TABLE "agentic-platform".build_status ADD COLUMN phase VARCHAR(100)')
        if 'step_number' not in existing_columns:
            cursor.execute('ALTER TABLE "agentic-platform".build_status ADD COLUMN step_number INTEGER')
        if 'duration_ms' not in existing_columns:
            cursor.execute('ALTER TABLE "agentic-platform".build_status ADD COLUMN duration_ms INTEGER')
        
        connection.commit()
    except Exception as e:
        connection.rollback()
        print(f"Schema update error: {e}")
    finally:
        cursor.close()
        connection.close()

# Ensure schema is updated on startup
@app.on_event("startup")
async def startup_event():
    ensure_schema_updated()

@app.post("/build-status", response_model=BuildStatus, status_code=status.HTTP_201_CREATED)
def create_build_status(item: BuildStatusCreate):
    connection = get_db_connection()
    cursor = connection.cursor()
    
    # Automatically determine phase from step if not provided
    if not item.phase and item.step:
        if "Phase" in item.step:
            item.phase = item.step
        elif "BuildProcess" == item.step:
            item.phase = "Overall"
        else:
            # Extract phase from step names based on common patterns
            phases = {
                "Install": ["InstallDependencies", "ECRLogin"],
                "PreBuild": ["DownloadAgent", "ExtractAgent"],
                "Build": ["BuildImage"],
                "PostBuild": ["TagImage", "PushImage"]
            }
            
            for phase, steps in phases.items():
                if any(step_name in item.step for step_name in steps):
                    item.phase = f"{phase}Phase"
                    break
    
    # Auto-set step number if not provided
    if item.step_number is None:
        # Get existing steps for this build to determine numbering
        cursor.execute(
            """
            SELECT step, step_number FROM "agentic-platform".build_status 
            WHERE build_id = %s AND step = %s
            ORDER BY id DESC LIMIT 1
            """, 
            (item.build_id, item.step)
        )
        result = cursor.fetchone()
        
        if result and result['step_number'] is not None:
            item.step_number = result['step_number']
        else:
            # Assign new step number
            cursor.execute(
                """
                SELECT MAX(step_number) as max_step FROM "agentic-platform".build_status 
                WHERE build_id = %s
                """, 
                (item.build_id,)
            )
            max_step = cursor.fetchone()[0] or 0
            item.step_number = max_step + 1
    
    # Calculate duration for "Success" or "Failed" statuses by finding the "Started" event
    if item.status in ["Success", "Failed"] and not item.duration_ms:
        cursor.execute(
            """
            SELECT timestamp FROM "agentic-platform".build_status 
            WHERE build_id = %s AND step = %s AND status = 'Started'
            ORDER BY id DESC LIMIT 1
            """, 
            (item.build_id, item.step)
        )
        start_record = cursor.fetchone()
        
        if start_record:
            start_time = start_record[0]
            duration = (item.timestamp - start_time).total_seconds() * 1000
            item.duration_ms = int(duration)
    
    try:
        cursor.execute(
            """
            INSERT INTO "agentic-platform".build_status 
            (agent_version_id, build_id, phase, step, step_number, status, message, timestamp, environment, duration_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (item.agent_version_id, item.build_id, item.phase, item.step, item.step_number, 
             item.status, item.message, item.timestamp, item.environment, item.duration_ms)
        )
        new_id = cursor.fetchone()[0]
        connection.commit()
        return {**item.dict(), "id": new_id}
    except Exception as e:
        connection.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()

@app.get("/build-status", response_model=List[BuildStatus])
def get_build_statuses(
    build_id: Optional[str] = None, 
    environment: Optional[str] = None, 
    phase: Optional[str] = None,
    status: Optional[str] = None,
    step: Optional[str] = None,
    limit: int = 50, 
    offset: int = 0
):
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        query = 'SELECT * FROM "agentic-platform".build_status WHERE 1=1 '
        params = []
        
        if build_id:
            query += " AND build_id = %s"
            params.append(build_id)
        if environment:
            query += " AND environment = %s"
            params.append(environment)
        if phase:
            query += " AND phase = %s"
            params.append(phase)
        if status:
            query += " AND status = %s"
            params.append(status)
        if step:
            query += " AND step = %s"
            params.append(step)
            
        query += " ORDER BY timestamp DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        
        cursor.execute(query, tuple(params))
        result = cursor.fetchall()
        return [dict(row) for row in result]
    finally:
        cursor.close()
        connection.close()

@app.get("/build-status/summary", response_model=BuildStatusSummary)
def get_build_status_summary(build_id: str, environment: Optional[str] = None):
    """Get a summarized view of the build process organized by phases and steps"""
    
    connection = get_db_connection()
    cursor = connection.cursor()
    
    try:
        query = """
            SELECT * FROM "agentic-platform".build_status 
            WHERE build_id = %s
        """
        params = [build_id]
        
        if environment:
            query += " AND environment = %s"
            params.append(environment)
            
        query += " ORDER BY timestamp ASC"
        
        cursor.execute(query, tuple(params))
        records = cursor.fetchall()
        
        if not records:
            raise HTTPException(status_code=404, detail=f"No build status found for build_id: {build_id}")
        
        # Initialize summary
        summary = {
            "build_id": build_id,
            "environment": records[0]["environment"],
            "overall_status": "In Progress",
            "phases": {},
            "started_at": None,
            "completed_at": None,
            "total_duration_ms": None
        }
        
        # Track overall process
        overall_started = None
        overall_completed = None
        
        for record in records:
            phase_name = record["phase"] or "Unknown"
            step_name = record["step"]
            
            # Initialize phase if not exists
            if phase_name not in summary["phases"]:
                summary["phases"][phase_name] = {
                    "status": "Not Started",
                    "steps": {},
                    "started_at": None,
                    "completed_at": None,
                    "duration_ms": None
                }
            
            phase = summary["phases"][phase_name]
            
            # Initialize step if not exists
            if step_name not in phase["steps"]:
                phase["steps"][step_name] = {
                    "status": record["status"],
                    "step_number": record["step_number"],
                    "message": record["message"],
                    "started_at": None,
                    "completed_at": None,
                    "duration_ms": record["duration_ms"]
                }
            
            step = phase["steps"][step_name]
            
            # Update step status
            step["status"] = record["status"]
            step["message"] = record["message"]
            
            # Track timestamps
            if record["status"] == "Started":
                if step["started_at"] is None:
                    step["started_at"] = record["timestamp"]
                
                if phase["started_at"] is None:
                    phase["started_at"] = record["timestamp"]
                
                if step_name == "BuildProcess" and overall_started is None:
                    overall_started = record["timestamp"]
                    summary["started_at"] = record["timestamp"]
            
            elif record["status"] in ["Success", "Failed"]:
                step["completed_at"] = record["timestamp"]
                step["duration_ms"] = record["duration_ms"]
                
                # Check if this is the last step in the phase
                if all(s["status"] in ["Success", "Failed"] for s in phase["steps"].values()):
                    phase["completed_at"] = record["timestamp"]
                    
                    # Calculate phase duration
                    if phase["started_at"]:
                        phase["duration_ms"] = int((record["timestamp"] - phase["started_at"]).total_seconds() * 1000)
                
                # Check if this is the final build process step
                if step_name == "BuildProcess":
                    overall_completed = record["timestamp"]
                    summary["completed_at"] = record["timestamp"]
                    summary["overall_status"] = record["status"]
            
            # Update phase status
            if any(s["status"] == "Failed" for s in phase["steps"].values()):
                phase["status"] = "Failed"
            elif all(s["status"] == "Success" for s in phase["steps"].values()):
                phase["status"] = "Success"
            elif any(s["status"] == "Started" for s in phase["steps"].values()):
                phase["status"] = "In Progress"
            
        # Calculate total duration if process completed
        if overall_started and overall_completed:
            summary["total_duration_ms"] = int((overall_completed - overall_started).total_seconds() * 1000)
        
        # Set overall status
        if any(p["status"] == "Failed" for p in summary["phases"].values()):
            summary["overall_status"] = "Failed"
        elif all(p["status"] == "Success" for p in summary["phases"].values() if p != "Overall"):
            summary["overall_status"] = "Success"
        
        return summary
    finally:
        cursor.close()
        connection.close()

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/agent-version", response_model=dict)
def get_agent_version(agent_id: str = Query(..., description="Agent ID"), version: str = Query(..., description="Version number")):
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        query = '''
            SELECT id FROM "agentic-platform".agent_versions 
            WHERE agent_id = %s AND version = %s
        '''
        cursor.execute(query, (agent_id, version))
        result = cursor.fetchone()
        
        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Agent version not found for agent_id {agent_id} and version {version}"
            )
            
        return {"id": result["id"]}
    except Exception as e:
        if not isinstance(e, HTTPException):
            raise HTTPException(status_code=500, detail=str(e))
        raise e
    finally:
        cursor.close()
        connection.close()

@app.post("/build-info", status_code=status.HTTP_201_CREATED)
def create_or_update_build_info(item: BuildInfo):
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        # Check if build_info already exists
        cursor.execute(
            """
            SELECT id FROM "agentic-platform".build_info
            WHERE agent_version_id = %s AND agent_uuid = %s AND version = %s
            """,
            (item.agent_version_id, item.agent_uuid, item.version)
        )
        existing = cursor.fetchone()

        if existing:
            # Update existing entry
            cursor.execute(
                """
                UPDATE "agentic-platform".build_info
                SET image_url = %s, timestamp = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (item.image_url, existing["id"])
            )
        else:
            # Insert new entry
            cursor.execute(
                """
                INSERT INTO "agentic-platform".build_info (agent_version_id, agent_uuid, version, image_url)
                VALUES (%s, %s, %s, %s)
                """,
                (item.agent_version_id, item.agent_uuid, item.version, item.image_url)
            )

        connection.commit()
        return {"message": "Build info saved successfully."}
    
    except Exception as e:
        connection.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        cursor.close()
        connection.close()


@app.get("/build-info", response_model=List[BuildInfoResponse])
def get_build_info(
    agent_uuid: Optional[str] = Query(None, description="Filter by Build ID"),
    agent_version_id: Optional[str] = Query(None, description="Filter by Agent Version ID"),
    version: Optional[str] = Query(None, description="Filter by Version"),
    limit: int = 50,
    offset: int = 0
):
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        query = '''
            SELECT id, agent_version_id, agent_uuid, version, image_url, timestamp
            FROM "agentic-platform".build_info
            WHERE 1=1
        '''
        params: List[Any] = []

        if agent_uuid:
            query += " AND agent_uuid = %s"
            params.append(agent_uuid)
        if agent_version_id:
            query += " AND agent_version_id = %s"
            params.append(agent_version_id)
        if version:
            query += " AND version = %s"
            params.append(version)

        query += " ORDER BY timestamp DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        cursor.execute(query, tuple(params))
        result = cursor.fetchall()
        return [dict(row) for row in result]

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        cursor.close()
        connection.close()
