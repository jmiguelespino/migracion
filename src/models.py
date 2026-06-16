from pydantic import BaseModel
from datetime import date, datetime

class Menues(BaseModel):
    numero: float
    menu: str
    imagen: str
    conteclas: str

class Movim(BaseModel):
    fecha: date
    cod_serv: float
    cod_empr: float
    cod_clie: float
    cod_cbte: str
    num_cbte: float
    cantidad: float
    proceso: datetime

class Programa(BaseModel):
    nombre: str
    menu: str
    descrip: str
    nmenu: float
    smenu: float
    tipo: str
    opcion: float