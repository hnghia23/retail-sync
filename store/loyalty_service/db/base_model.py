from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, DateTime, func

# Tạo Base class để các model khác kế thừa
Base = declarative_base()


class BaseModel:
    """
    BaseModel chứa các cột chung (nếu có)
    và các hàm tiện ích để chuyển đối tượng ORM thành dict.
    """

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def as_dict(self):
        """Chuyển object SQLAlchemy thành dictionary"""
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

    def __repr__(self):
        """Chuẩn hóa hiển thị khi in object"""
        return f"<{self.__class__.__name__}({self.as_dict()})>"
