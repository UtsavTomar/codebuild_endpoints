from fastapi import FastAPI, HTTPException, Depends, Query, status, Security, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from typing import List, Optional
from datetime import datetime
import databases
import sqlalchemy
from sqlalchemy import and_, desc
import os
import psycopg2
from psycopg2.extras import Json

# Configuration
DATABASE_URL = os.getenv("DB_CONNECTION_STRING")

# Database connection
database = databases.Database(DATABASE_URL)

# JWT Token validation function
async def validate_token(request: Request):
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    return auth_header.split('Bearer ')[1]

# Database connection function
def get_db_connection():
    DB_CONNECTION_STRING = os.getenv("DB_CONNECTION_STRING")
    if not DB_CONNECTION_STRING:
        raise Exception("Database connection string not found")
    return psycopg2.connect(DB_CONNECTION_STRING)

# JWT token verification function
def verify_jwt_token(cursor, jwt_token):
    cursor.execute("""
        SELECT EXISTS(
            SELECT 1
            FROM "agentic-platform".jwt_tokens
            WHERE jwt_token = %s
        )
    """, (jwt_token,))
    return cursor.fetchone()[0]

# SQLAlchemy metadata
metadata = sqlalchemy.MetaData()

# Define build_status table
build_status = sqlalchemy.Table(
    "build_status",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("agent_version_id", sqlalchemy.String, index=True),
    sqlalchemy.Column("build_id", sqlalchemy.String, index=True),
    sqlalchemy.Column("step", sqlalchemy.String),
    sqlalchemy.Column("status", sqlalchemy.String),
    sqlalchemy.Column("message", sqlalchemy.Text),
    sqlalchemy.Column("timestamp", sqlalchemy.DateTime, default=datetime.utcnow),
    sqlalchemy.Column("environment", sqlalchemy.String, index=True),
)

# Pydantic models
class BuildStatusBase(BaseModel):
    agent_version_id: str
    build_id: str
    step: str
    status: str
    message: str
    timestamp: datetime
    environment: str


class BuildStatusCreate(BuildStatusBase):
    pass


class BuildStatus(BuildStatusBase):
    id: int

    model_config = ConfigDict(from_attributes=True)


class BuildSummary(BaseModel):
    build_id: str
    agent_version_id: str
    environment: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    steps_total: int
    steps_completed: int
    steps_failed: int


class ErrorResponse(BaseModel):
    detail: str


# FastAPI app
app = FastAPI(
    title="Agent Build Status API",
    description="API for tracking and retrieving agent build statuses",
    version="1.0.0",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    try:
        await database.connect()
        print("✅ Database Connected Successfully!")
    except Exception as e:
        print(f"❌ Database Connection Failed: {e}")


@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()


@app.post("/build-status", response_model=BuildStatus, status_code=status.HTTP_201_CREATED)
async def create_build_status(
    item: BuildStatusCreate, jwt_token: str = Depends(validate_token)
):
    
    print("Received Data:", item.dict())  # Debugging
    try:
        query = build_status.insert().values(**item.dict())
        last_record_id = await database.execute(query)
        return {**item.dict(), "id": last_record_id}
    except Exception as e:
        print(f"❌ Insert Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/build-status", response_model=List[BuildStatus])
async def get_build_statuses(
    build_id: Optional[str] = None,
    agent_version_id: Optional[str] = None,
    environment: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    jwt_token: str = Depends(validate_token),
):
    """
    Get build status entries with optional filtering.
    """
    query = build_status.select()
    
    # Apply filters
    filters = []
    if build_id:
        filters.append(build_status.c.build_id == build_id)
    if agent_version_id:
        filters.append(build_status.c.agent_version_id == agent_version_id)
    if environment:
        filters.append(build_status.c.environment == environment)
    if status:
        filters.append(build_status.c.status == status)
    
    if filters:
        query = query.where(and_(*filters))
    
    # Apply pagination
    query = query.order_by(desc(build_status.c.timestamp)).limit(limit).offset(offset)
    
    return await database.fetch_all(query)


@app.get("/build-status/{build_id}", response_model=List[BuildStatus])
async def get_build_status_by_id(
    build_id: str, jwt_token: str = Depends(validate_token)
):
    """
    Get all status entries for a specific build ID.
    """
    query = build_status.select().where(build_status.c.build_id == build_id).order_by(build_status.c.timestamp)
    result = await database.fetch_all(query)
    
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Build ID {build_id} not found",
        )
    
    return result


@app.get("/build-summary", response_model=List[BuildSummary])
async def get_build_summaries(
    environment: Optional[str] = None,
    agent_version_id: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    jwt_token: str = Depends(validate_token),
):
    """
    Get summaries of builds, including overall status and progress.
    """
    # First, get unique build IDs based on filters
    build_id_query = sqlalchemy.select([
        build_status.c.build_id,
        build_status.c.agent_version_id,
        build_status.c.environment
    ]).distinct()
    
    # Apply filters
    filters = []
    if environment:
        filters.append(build_status.c.environment == environment)
    if agent_version_id:
        filters.append(build_status.c.agent_version_id == agent_version_id)
    
    if filters:
        build_id_query = build_id_query.where(and_(*filters))
    
    # Apply pagination
    build_id_query = build_id_query.order_by(desc(build_status.c.timestamp)).limit(limit).offset(offset)
    
    build_ids = await database.fetch_all(build_id_query)
    
    summaries = []
    for build in build_ids:
        # Get all status entries for this build
        status_query = build_status.select().where(
            build_status.c.build_id == build.build_id
        ).order_by(build_status.c.timestamp)
        
        status_entries = await database.fetch_all(status_query)
        
        if not status_entries:
            continue
        
        # Calculate summary data
        first_entry = status_entries[0]
        last_entry = status_entries[-1]
        
        # Count steps by status
        steps_total = len(status_entries)
        steps_completed = sum(1 for entry in status_entries if entry.status == "Success")
        steps_failed = sum(1 for entry in status_entries if entry.status == "Failed")
        
        # Determine overall build status
        if any(entry.status == "Failed" for entry in status_entries):
            overall_status = "Failed"
        elif all(entry.status in ["Success", "Skipped"] for entry in status_entries):
            overall_status = "Success"
        else:
            overall_status = "In Progress"
        
        # Calculate duration if build is completed
        duration_seconds = None
        completed_at = None
        
        if overall_status in ["Success", "Failed"]:
            completed_at = last_entry.timestamp
            duration_seconds = (completed_at - first_entry.timestamp).total_seconds()
        
        # Create summary
        summary = BuildSummary(
            build_id=build.build_id,
            agent_version_id=build.agent_version_id,
            environment=build.environment,
            status=overall_status,
            started_at=first_entry.timestamp,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
            steps_total=steps_total,
            steps_completed=steps_completed,
            steps_failed=steps_failed,
        )
        
        summaries.append(summary)
    
    return summaries


@app.get("/build-latest", response_model=BuildSummary)
async def get_latest_build(
    environment: Optional[str] = None,
    agent_version_id: Optional[str] = None,
    jwt_token: str = Depends(validate_token),
):
    """
    Get the latest build summary for the specified environment and agent version.
    """
    # Use the same logic as get_build_summaries but with limit=1
    summaries = await get_build_summaries(
        environment=environment,
        agent_version_id=agent_version_id,
        limit=1,
        offset=0,
        jwt_token=jwt_token,
    )
    
    if not summaries:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No builds found matching the criteria",
        )
    
    return summaries[0]


@app.get("/health")
async def health_check():
    """
    Health check endpoint that doesn't require authentication.
    """
    return {"status": "healthy"}
