# -*- coding: utf-8 -*-
"""
===================================
分析服务层
===================================

职责：
1. 封装股票分析逻辑
2. 调用 pipeline 执行分析
3. 统一构建 API 响应
"""

import logging
import uuid
from typing import Any, Dict, Optional

from src.repositories.analysis_repo import AnalysisRepository
from src.report_language import (
    get_localized_stock_name,
    get_sentiment_label,
    localize_operation_advice,
    localize_trend_prediction,
    normalize_report_language,
)
from src.services.name_to_code_resolver import resolve_stock_input

logger = logging.getLogger(__name__)


class AnalysisService:
    """封装股票分析相关业务逻辑。"""

    def __init__(self):
        self.repo = AnalysisRepository()

    def analyze_stock(
        self,
        stock_code: str,
        report_type: str = "detailed",
        force_refresh: bool = False,
        query_id: Optional[str] = None,
        send_notification: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        执行股票分析。

        Args:
            stock_code: 用户输入的股票代码或公司名
            report_type: 报告类型
            force_refresh: 是否强制刷新
            query_id: 查询 ID
            send_notification: 是否发送通知
        """
        resolved_stock_code = resolve_stock_input(stock_code)
        if not resolved_stock_code:
            logger.error("无法识别股票输入: %s", stock_code)
            return None

        try:
            from src.config import get_config
            from src.core.pipeline import StockAnalysisPipeline
            from src.enums import ReportType

            if query_id is None:
                query_id = uuid.uuid4().hex

            config = get_config()
            pipeline = StockAnalysisPipeline(
                config=config,
                query_id=query_id,
                query_source="api",
            )

            rt = ReportType.from_str(report_type)
            result = pipeline.process_single_stock(
                code=resolved_stock_code,
                skip_analysis=False,
                single_stock_notify=send_notification,
                report_type=rt,
            )

            if result is None:
                logger.warning("分析股票 %s 返回空结果", resolved_stock_code)
                return None

            return self._build_analysis_response(result, query_id, report_type=rt.value)

        except Exception as exc:
            logger.error("分析股票 %s 失败: %s", resolved_stock_code, exc, exc_info=True)
            return None

    def _build_analysis_response(
        self,
        result: Any,
        query_id: str,
        report_type: str = "detailed",
    ) -> Dict[str, Any]:
        """构建统一分析响应。"""
        sniper_points = {}
        if hasattr(result, "get_sniper_points"):
            sniper_points = result.get_sniper_points() or {}

        report_language = normalize_report_language(getattr(result, "report_language", "zh"))
        sentiment_label = get_sentiment_label(result.sentiment_score, report_language)
        stock_name = get_localized_stock_name(getattr(result, "name", None), result.code, report_language)

        report = {
            "meta": {
                "query_id": query_id,
                "stock_code": result.code,
                "stock_name": stock_name,
                "report_type": report_type,
                "report_language": report_language,
                "current_price": result.current_price,
                "change_pct": result.change_pct,
                "model_used": getattr(result, "model_used", None),
            },
            "summary": {
                "analysis_summary": result.analysis_summary,
                "operation_advice": localize_operation_advice(result.operation_advice, report_language),
                "trend_prediction": localize_trend_prediction(result.trend_prediction, report_language),
                "sentiment_score": result.sentiment_score,
                "sentiment_label": sentiment_label,
            },
            "strategy": {
                "ideal_buy": sniper_points.get("ideal_buy"),
                "secondary_buy": sniper_points.get("secondary_buy"),
                "stop_loss": sniper_points.get("stop_loss"),
                "take_profit": sniper_points.get("take_profit"),
            },
            "details": {
                "news_summary": result.news_summary,
                "technical_analysis": result.technical_analysis,
                "fundamental_analysis": result.fundamental_analysis,
                "risk_warning": result.risk_warning,
            },
        }

        return {
            "stock_code": result.code,
            "stock_name": stock_name,
            "report": report,
        }
