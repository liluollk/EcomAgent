"""能力组装层(计划 02 Task 2;架构文档 §3、§4)。

- ``registry``:进程内版本 Registry,只按稳定 Key 与 Version ID 查找;
- ``validation``:启动校验与 ``AssemblyError``(带确定性 reason_code);
- ``assembly``:RuntimeAssembly 聚合注册表并执行启动校验与版本解析;
- ``run_factory``:Run 创建时钉住实际解析的版本;
- ``write_service``:写前读当前授权的最小执行服务。
"""
