from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float, Boolean, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.session import Base


class DocumentTemplate(Base):
    """PDF template definition with background image and coordinate-mapped fields.
    
    This is the core of the Overlay Engine seen in the reference video.
    Background images (visa form, bank slip, etc.) are stored in /static/pdf_backgrounds/
    """
    __tablename__ = "document_templates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text, default="")
    category = Column(String(50), default="custom")  # visa, bank, government, custom
    data_source = Column(String(50), default="candidate")  # candidate, demand, client, agent, custom
    background_image = Column(String(255), default="")  # filename in pdf_backgrounds/
    page_width = Column(Float, default=595)   # A4 width in points
    page_height = Column(Float, default=842)  # A4 height in points
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    fields = relationship("DocumentField", back_populates="template", cascade="all, delete-orphan")


class DocumentField(Base):
    """A coordinate-mapped field on a DocumentTemplate.
    
    field_key is mapped to a real database column based on data_source.
    Example: data_source=candidate, field_key=full_name -> reads Candidate.full_name
    """
    __tablename__ = "document_fields"

    id = Column(Integer, primary_key=True, index=True)
    template_id = Column(Integer, ForeignKey("document_templates.id"), nullable=False)
    label = Column(String(150), nullable=False)
    field_key = Column(String(100), nullable=False)  # e.g. full_name, passport_no
    # field_type: text | date | checkbox | static | char_cells | photo | barcode | arabic | trade_table
    field_type = Column(String(30), default="text")
    static_value = Column(Text, default="")  # used when field_type='static'
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    width = Column(Float, default=200)
    height = Column(Float, default=20)
    font_size = Column(Float, default=11)
    font_bold = Column(Boolean, default=False)
    font_italic = Column(Boolean, default=False)
    color = Column(String(10), default="#000000")
    align = Column(String(10), default="left")
    page = Column(Integer, default=1)
    # Extra params for advanced field types:
    #   char_cells: { "cell_count": 13, "cell_width": 18, "cell_gap": 2 }
    #   photo:      { "fit": "cover", "border": true }
    #   barcode:    { "format": "code128", "show_text": true }
    #   arabic:     { "rtl": true, "shape": true }
    #   trade_table:{ "cols": ["category","qty","assigned","available"], "row_height": 18 }
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, server_default=func.now())

    template = relationship("DocumentTemplate", back_populates="fields")


class GeneratedDocument(Base):
    """Log of every generated PDF."""
    __tablename__ = "generated_documents"

    id = Column(Integer, primary_key=True, index=True)
    template_id = Column(Integer, ForeignKey("document_templates.id"), nullable=False)
    record_id = Column(Integer, nullable=True)  # ID from data_source table (e.g. candidate id)
    file_path = Column(String(500), default="")
    generated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    generated_at = Column(DateTime, server_default=func.now())
