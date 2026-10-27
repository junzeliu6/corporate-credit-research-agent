from __future__ import annotations

import json

from credit_research_agent.company_scope import (
    CompanyScopeStatus,
    FinancialEnterpriseType,
    IndustryMetadata,
    assess_company_scope,
)


def write_pages(tmp_path, *texts: str):
    path = tmp_path / "pages.jsonl"
    path.write_text(
        "".join(
            json.dumps(
                {
                    "page_number": index,
                    "text": text,
                    "char_count": len(text),
                    "status": "extracted",
                },
                ensure_ascii=False,
            )
            + "\n"
            for index, text in enumerate(texts, start=1)
        ),
        encoding="utf-8",
    )
    return path


def test_verified_financial_industry_is_rejected_without_name_guessing(tmp_path):
    pages = write_pages(tmp_path, "公司从事的主要业务\n普通软件服务")
    result = assess_company_scope(
        pages_path=pages,
        industry_metadata=IndustryMetadata(
            industry_code="J67",
            industry_name="资本市场服务",
            industry_standard="证监会行业分类",
            verified=True,
            source_url="https://example.test/master",
        ),
    )
    assert result.status is CompanyScopeStatus.UNSUPPORTED_FINANCIAL_ENTERPRISE


def test_company_name_is_never_used_as_financial_signal(tmp_path):
    pages = write_pages(
        tmp_path,
        "公司从事的主要业务\n证券纸张和包装材料制造",
        "合并资产负债表\n存货 100\n应收账款 50",
        "合并利润表\n营业收入 200\n营业成本 100",
    )
    result = assess_company_scope(pages_path=pages)
    assert result.status is CompanyScopeStatus.SUPPORTED_NON_FINANCIAL
    assert result.financial_enterprise_type is None


def test_bank_requires_business_and_statement_signals(tmp_path):
    pages = write_pages(
        tmp_path,
        "报告期内公司从事的主要业务\n本行从事吸收存款和发放贷款业务",
        "合并资产负债表\n发放贷款和垫款 500\n吸收存款 800",
        "合并利润表\n利息净收入 120",
    )
    result = assess_company_scope(pages_path=pages)
    assert result.status is CompanyScopeStatus.UNSUPPORTED_FINANCIAL_ENTERPRISE
    assert result.financial_enterprise_type is FinancialEnterpriseType.BANK
    assert result.business_signals
    assert result.statement_signals


def test_securities_and_insurance_are_detected_with_two_signal_types(tmp_path):
    securities = write_pages(
        tmp_path,
        "主营业务\n公司开展证券经纪、证券承销和融资融券业务",
        "合并资产负债表\n代理买卖证券款 500\n融出资金 300",
        "合并利润表\n手续费及佣金净收入 100",
    )
    result = assess_company_scope(pages_path=securities)
    assert result.financial_enterprise_type is FinancialEnterpriseType.SECURITIES

    insurance_dir = tmp_path / "insurance"
    insurance_dir.mkdir()
    insurance = write_pages(
        insurance_dir,
        "主要业务\n公司经营人身保险和承保业务",
        "合并资产负债表\n保险合同准备金 500",
        "合并利润表\n已赚保费 200\n赔付支出 80",
    )
    result = assess_company_scope(pages_path=insurance)
    assert result.financial_enterprise_type is FinancialEnterpriseType.INSURANCE


def test_financial_keyword_in_note_does_not_trigger_jiuzhoutong_false_positive(
    tmp_path,
):
    pages = write_pages(
        tmp_path,
        "公司从事的主要业务\n医药分销、医药零售和物流服务",
        "合并资产负债表\n存货 100\n应收账款 50",
        "合并利润表\n营业收入 200\n营业成本 100",
        "第十节 财务报告 附注\n联营企业从事金融租赁及相关业务，利息净收入未并表",
    )
    result = assess_company_scope(pages_path=pages)
    assert result.status is CompanyScopeStatus.SUPPORTED_NON_FINANCIAL
    assert not result.business_signals


def test_single_financial_signal_is_not_enough_to_reject(tmp_path):
    pages = write_pages(
        tmp_path,
        "主营业务\n公司曾投资证券公司，但主营为设备制造",
        "合并资产负债表\n存货 100\n应收账款 50",
        "合并利润表\n营业收入 200\n营业成本 100",
    )
    result = assess_company_scope(pages_path=pages)
    assert result.status is CompanyScopeStatus.SUPPORTED_NON_FINANCIAL


def test_verified_nonfinancial_master_conflicting_with_financial_report_is_flagged(
    tmp_path,
):
    pages = write_pages(
        tmp_path,
        "主营业务\n公司开展证券经纪业务",
        "合并资产负债表\n代理买卖证券款 500",
        "合并利润表\n手续费及佣金净收入 100",
    )
    result = assess_company_scope(
        pages_path=pages,
        industry_metadata=IndustryMetadata(
            industry_code="C39",
            industry_name="计算机、通信和其他电子设备制造业",
            verified=True,
        ),
    )
    assert result.status is CompanyScopeStatus.INDUSTRY_EVIDENCE_CONFLICT


def test_missing_industry_and_insufficient_report_evidence_is_unconfirmed(tmp_path):
    pages = write_pages(tmp_path, "年度报告\n未能定位主营业务及合并主表")
    result = assess_company_scope(pages_path=pages)
    assert result.status is CompanyScopeStatus.ENTERPRISE_SCOPE_UNCONFIRMED

