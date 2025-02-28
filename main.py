from fastapi import FastAPI, HTTPException, Depends, Query, status, Security, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
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
    step: str
    status: str
    message: str
    timestamp: datetime
    environment: str

class BuildStatusCreate(BaseModel):
    agent_version_id: str
    build_id: str
    step: str
    status: str
    message: str
    timestamp: datetime
    environment: str

@app.post("/build-status", response_model=BuildStatus, status_code=status.HTTP_201_CREATED)
def create_build_status(item: BuildStatusCreate):
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO "agentic-platform".build_status (agent_version_id, build_id, step, status, message, timestamp, environment)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (item.agent_version_id, item.build_id, item.step, item.status, item.message, item.timestamp, item.environment)
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
def get_build_statuses(build_id: Optional[str] = None, environment: Optional[str] = None, limit: int = 50, offset: int = 0):
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
        query += " ORDER BY timestamp DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        
        cursor.execute(query, tuple(params))
        result = cursor.fetchall()
        return [dict(row) for row in result]
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
