import os, sys
import json
import glob
import re
import shutil
from pathlib import Path
import subprocess
from rich import print
import hcl2
import click
from git import Repo

def construct_dag():
    pass

def gitClone(repo, path):
    shutil.rmtree(path,ignore_errors=True)
    Repo.clone_from(repo,path)

def parsetform(path):
    res = {}
    for filename in glob.glob(os.path.join(path, "*.tf")):
        with open(filename, "r") as f:
            d = hcl2.load(f)
            res.update(d)
    return res

def genVars(path, variables):
    with open(os.path.join(path, "genie_variables.tf"), "a") as f:
        for key, val in variables.items():
            f.write(f"""
variable "{key}" {{
    default = "{val}"
}}
""")

def genLocals(path, locals):
    with open(os.path.join(path, "genie_locals.tf"), "w") as f:
        f.write("locals ")
        json.dump(locals, f, indent=2, separators=[",", " = "])

    with open(os.path.join(path, "genie_locals.tf"), "r") as f:
        fr = f.read()

    with open(os.path.join(path, "genie_locals.tf"), "w") as f:
        fr = re.sub(',\n','\n', fr)
        fr = re.sub('\"(?P<all>.*)\" = ','\g<all> = ', fr)
        f.write(fr)

def genInputs(path, data, injects, resolved_outputs):
    with open(os.path.join(path, "genie_inputs.tf"), "a") as f:
        for output in resolved_outputs:
            t = output["type"]
            n = output["name"]
            rn = output["resource_name"]
            output_id = output["id"]
            # ec2 instances have different 'id' keys
            if t == "aws_instance":
                id_type = "instance_id"
            else:
                id_type = "id"
            f.write(f"""
data "{t}" "{n}" {{
    {id_type} = "{output_id}"
}}

    """)

def genoutputs(path, resources, injects):
    outputs = []
    with open(os.path.join(path, "genie_outputs.tf"), "a") as f:
        resource_names = list(map(lambda x: x["resource_name"], injects))
        names = list(map(lambda x: x["name"], injects))
        types = list(map(lambda x: x["type"], injects))
        for i in range(len(injects)):
            t = types[i]
            rn = resource_names[i]
            n = names[i]
            # we need to look for [ { type: { resource_name: {} } } ]
            # to find things like aws_instance.this
            if list(filter(lambda q: q.get(t, {}).get(rn), resources)):
                outputs.append({
                    "resource_name": rn,
                    "name": n,
                    "type": t
                })
                f.write(f"""
output "{rn}_id"{{
    value = {t}.{rn}.id
}}
    
""")
    return outputs

def applyAndResolveOutputs(path, outputs, pipelineName):
    cdir = os.getcwd()
    os.chdir(path)
    subprocess.run(["terraform", "init"], check=True)
    subprocess.run(["terraform", "apply", "-auto-approve", "-state", f"../../{pipelineName}.terraform.tfstate"], check=True)
    proc = subprocess.run(["terraform", "output", "-json", "-state", f"../../{pipelineName}.terraform.tfstate"], capture_output=True)
    terraform_outputs = proc.stdout.decode("utf-8").strip()
    terraform_outputs = json.loads(terraform_outputs)
    resolved = []
    for output in outputs:
        n = output["name"]
        rn = output["resource_name"]
        t = output["type"]
        resolved.append({
            "id": terraform_outputs[f"{rn}_id"]["value"],
            "type": t,
            "name": n,
            "resource_name": rn,
        })

    os.chdir(cdir)
    return resolved

def destroyInfra(modulePath, pipelineName):
    cdir = os.getcwd()
    os.chdir(modulePath)
    subprocess.run(["terraform", "init"], check=True)
    subprocess.run(["terraform", "destroy", "-auto-approve", "-state", f"../../{pipelineName}.terraform.tfstate"], check=True)
    os.chdir(cdir)
    shutil.rmtree(modulePath)
    for file in glob.glob(os.path.join(cdir, f"{pipelineName}.terraform.tfstate*")):
        os.remove(file)

GENIESPLASH = """
🧞 InfraGenie 😄

"""

@click.group(epilog=GENIESPLASH)
def cli():
    print(":vampire: welcome to [bold magenta]InfraGenie[/bold magenta] CLI! :smile:")


@cli.command()
def apply():

    print("parsing genie.hcl file ...")

    if not os.path.exists("genie.hcl"):
        print("[bold red]Error! genie.hcl file not found[/bold red]")
        sys.exit(1)

    with open("genie.hcl") as f:
        d = hcl2.load(f)
        print("found the following settings:",d)
        globalVars = d.get("variables", [{}])[0]
        globalLocals = d.get("locals", [{}])[0]
    print("Rendering data outputs")
    injects = []
    for name, inject in d["inject"][0].items():
        source = inject["source"].replace("${", "").replace("}", "")
        print(name, source)
        module, rtype, rname, = source.split(".")
        injects.append({
            "name": name,
            "module": module,
            "type": rtype,
            "resource_name": rname
        })

    print("rendering terraform outputs")

    shutil.rmtree(".infragenie",ignore_errors=True)
    Path(".infragenie").mkdir(parents=True, exist_ok=True)
    resolved_outputs = []
    for pipeline in d["pipeline"][0]["steps"]:
        print("Creating pipeline step",pipeline["name"])
        pipelinename, pipelinesource = pipeline["name"], pipeline.get("source")
        if "git" in pipeline.keys():
            gitClone(pipeline["git"],pipelinesource)
        modulePath = os.path.join(f".infragenie/{pipeline['name']}")
        shutil.copytree(pipelinesource, modulePath)

        tformdata = parsetform(modulePath)
        tformRequires = tformdata.get("data", [{}])
        terraformResources = tformdata.get("resource", [{}])

        genVars(modulePath, globalVars)
        if "variables" in pipeline.keys():
            genVars(modulePath, pipeline["variables"])
        if "locals" in pipeline.keys():
            applyLocals = globalLocals | pipeline["locals"]
        else:
            applyLocals = globalLocals

        genInputs(modulePath, tformRequires, injects, resolved_outputs)
        genLocals(modulePath, applyLocals)
        outputs = genoutputs(modulePath, terraformResources, injects)
        tmpOutputs = applyAndResolveOutputs(modulePath, outputs, pipelinename)
        resolved_outputs.extend(tmpOutputs)


@click.command()
@click.argument('steplist', nargs=-1)
def destroy(steplist):
    if not os.path.exists("genie.hcl"):
        print("[bold red]Error! genie.hcl file not found[/bold red]")
        sys.exit(1)

    with open("genie.hcl") as f:
        d = hcl2.load(f)

    if not steplist: # this allows us to assume "destroy all" if 'steplist' unspecified
        steplist = []
        p_steplist = d["pipeline"][0]["steps"]
        p_steplist.reverse() # destroy all in reverse order to avoid dependency conflicts
        for pipeline in p_steplist:
            steplist.append(pipeline["name"])

    for name in steplist: # this does no dependency checks
        modulePath = f".infragenie/{name}"
        if not os.path.exists(modulePath):
            print("[bold red]'"+name+"' already gone![/bold red]")
            continue
        destroyInfra(modulePath, name)

cli.add_command(destroy)
